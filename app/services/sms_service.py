from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Invoice, SmsTransaction
from app.parsers import normalize_bank_code, parse_bank_sms
from app.services.invoice_service import confirm_invoice_paid


def sms_fingerprint(sender: str, message: str, device_id: str | None, merchant_id: int | None = None) -> str:
    raw = f"{merchant_id or 0}\n{sender}\n{device_id or ''}\n{message.strip()}"
    return sha256(raw.encode("utf-8")).hexdigest()


def normalize_source_id(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())


@dataclass(slots=True)
class SmsIngestDiagnostic:
    result: str
    detail: str
    amount_candidate_count: int = 0
    bank_candidate_count: int = 0
    source_candidate_count: int = 0


async def ingest_sms(
    session: AsyncSession,
    sender: str,
    message: str,
    device_id: str | None,
    merchant_id: int | None = None,
    bank_hint: str | None = None,
) -> tuple[SmsTransaction, Invoice | None, SmsIngestDiagnostic]:
    fingerprint = sms_fingerprint(sender, message, device_id, merchant_id)
    existing = await session.scalar(select(SmsTransaction).where(SmsTransaction.fingerprint == fingerprint))
    if existing and (existing.status == "matched" or existing.matched_invoice_id):
        return existing, None, SmsIngestDiagnostic("duplicate", "این پیامک قبلاً مصرف و به فاکتور متصل شده است")

    parsed = parse_bank_sms(sender, message, bank_hint=bank_hint)
    if existing:
        # پیامک‌های قبلی unmatched/review با Retry دوباره پردازش می‌شوند.
        sms = existing
        sms.sender = sender[:120]
        sms.device_id = device_id or None
        sms.raw_message = message
        sms.bank_code = parsed.bank_code
        sms.amount_rial = parsed.amount_rial
        sms.card_last4 = parsed.card_last4
        sms.transaction_at = parsed.transaction_at
        sms.reference_number = parsed.reference_number
        sms.parse_confidence = parsed.confidence
        sms.status = "received"
    else:
        sms = SmsTransaction(
            sender=sender[:120],
            device_id=(device_id or None),
            raw_message=message,
            bank_code=parsed.bank_code,
            amount_rial=parsed.amount_rial,
            card_last4=parsed.card_last4,
            transaction_at=parsed.transaction_at,
            reference_number=parsed.reference_number,
            fingerprint=fingerprint,
            parse_confidence=parsed.confidence,
            status="received",
        )
        session.add(sms)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(select(SmsTransaction).where(SmsTransaction.fingerprint == fingerprint))
            return existing, None, SmsIngestDiagnostic("duplicate", "اثر انگشت پیامک قبلاً ثبت شده است")

    if not parsed.is_credit:
        sms.status = "review"
        return sms, None, SmsIngestDiagnostic("not_credit", "متن پیامک به‌عنوان واریز قطعی تشخیص داده نشد")
    if not parsed.amount_rial:
        sms.status = "review"
        return sms, None, SmsIngestDiagnostic("amount_not_found", "مبلغ واریز از متن پیامک استخراج نشد")
    if parsed.confidence < 65:
        sms.status = "review"
        return sms, None, SmsIngestDiagnostic(
            "low_confidence", f"اطمینان Parser پایین است: {parsed.confidence}%"
        )

    now = datetime.now(timezone.utc)
    amount_candidates = list(
        (
            await session.scalars(
                select(Invoice)
                .where(
                    Invoice.status == "pending",
                    Invoice.payable_amount_rial == parsed.amount_rial,
                    Invoice.expires_at >= now,
                    *([Invoice.merchant_id == merchant_id] if merchant_id is not None else []),
                )
                .options(joinedload(Invoice.card), joinedload(Invoice.merchant))
            )
        ).all()
    )

    if not amount_candidates:
        sms.status = "unmatched"
        return sms, None, SmsIngestDiagnostic(
            "no_amount_candidate",
            f"فاکتور فعال با مبلغ دقیق {parsed.amount_rial} ریال پیدا نشد",
            amount_candidate_count=0,
        )

    parsed_bank = normalize_bank_code(parsed.bank_code)
    bank_candidates: list[Invoice] = []
    bank_mismatches: list[str] = []
    for invoice in amount_candidates:
        card_bank = normalize_bank_code(invoice.card.bank_code)
        if parsed_bank != "generic" and card_bank != parsed_bank:
            bank_mismatches.append(card_bank)
            continue
        if parsed.card_last4 and invoice.card.card_last4 != parsed.card_last4:
            continue
        bank_candidates.append(invoice)

    if not bank_candidates:
        sms.status = "unmatched"
        expected = ", ".join(sorted(set(bank_mismatches))) or "نامشخص"
        return sms, None, SmsIngestDiagnostic(
            "bank_or_card_mismatch",
            f"مبلغ پیدا شد اما بانک/کارت تطبیق نداشت؛ تشخیص={parsed_bank}، کارت‌های فاکتور={expected}",
            amount_candidate_count=len(amount_candidates),
            bank_candidate_count=0,
        )

    # device_id یک کمک برای تفکیک چند گوشی است، نه دلیل قطعی برای رد پرداخت.
    # در نسخه‌های قبلی اختلاف کوچک در نام منبع (phone-1 / Phone 1 و ...) باعث
    # رد شدن واریز معتبر می‌شد. ابتدا تطبیق دقیق منبع را ترجیح می‌دهیم؛ اگر
    # فقط یک فاکتور مبلغ+بانک وجود داشته باشد، اختلاف منبع مانع تأیید نمی‌شود.
    incoming_source = normalize_source_id(device_id)
    source_candidates = bank_candidates
    source_note = ""
    if incoming_source:
        exact_source = [
            invoice
            for invoice in bank_candidates
            if normalize_source_id(invoice.card.sms_source_id) == incoming_source
        ]
        cards_without_source = [invoice for invoice in bank_candidates if not normalize_source_id(invoice.card.sms_source_id)]
        if exact_source:
            source_candidates = exact_source
        elif cards_without_source:
            source_candidates = cards_without_source
            source_note = "منبع پیامک روی کارت ثبت نشده بود؛ تطبیق بر اساس مبلغ و بانک انجام شد"
        elif len(bank_candidates) == 1:
            source_candidates = bank_candidates
            source_note = (
                f"شناسه منبع متفاوت بود (ورودی={device_id!r}، کارت={bank_candidates[0].card.sms_source_id!r})؛ "
                "به دلیل یکتا بودن مبلغ و بانک نادیده گرفته شد"
            )
        else:
            sms.status = "review"
            return sms, None, SmsIngestDiagnostic(
                "source_mismatch_ambiguous",
                "چند فاکتور هم‌مبلغ وجود دارد و شناسه منبع پیامک با هیچ کارت تطبیق ندارد",
                amount_candidate_count=len(amount_candidates),
                bank_candidate_count=len(bank_candidates),
                source_candidate_count=0,
            )

    if len(source_candidates) != 1:
        sms.status = "review"
        return sms, None, SmsIngestDiagnostic(
            "ambiguous",
            f"{len(source_candidates)} فاکتور هم‌مبلغ و هم‌بانک پیدا شد؛ تأیید خودکار امن نیست",
            amount_candidate_count=len(amount_candidates),
            bank_candidate_count=len(bank_candidates),
            source_candidate_count=len(source_candidates),
        )

    invoice = await confirm_invoice_paid(session, source_candidates[0].id, sms.id, parsed.reference_number)
    if not invoice:
        sms.status = "review"
        return sms, None, SmsIngestDiagnostic(
            "race_or_already_paid",
            "فاکتور هم‌زمان تغییر وضعیت داده یا قبلاً پرداخت شده است",
            amount_candidate_count=len(amount_candidates),
            bank_candidate_count=len(bank_candidates),
            source_candidate_count=len(source_candidates),
        )

    sms.status = "matched"
    sms.matched_invoice_id = invoice.id
    detail = "فاکتور با مبلغ، بانک و حساب پذیرنده تطبیق داده شد"
    if source_note:
        detail += f"؛ {source_note}"
    return sms, invoice, SmsIngestDiagnostic(
        "matched",
        detail,
        amount_candidate_count=len(amount_candidates),
        bank_candidate_count=len(bank_candidates),
        source_candidate_count=len(source_candidates),
    )
