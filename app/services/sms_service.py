from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Invoice, SmsParserTemplate, SmsTransaction
from app.parsers import normalize_bank_code, parse_bank_sms
from app.parsers.base import ParsedSms, digits_only, normalize_text
from app.services.invoice_service import confirm_invoice_paid


def _normalize_sms_text(message: str) -> str:
    """Create a stable representation of the same bank SMS.

    SMS Forwarder may deliver the same message twice with a different sender label,
    extra whitespace, Persian/Arabic digits, or an added ``From:`` line.  Those
    transport differences must not create a second payment attempt.
    """
    text = unicodedata.normalize("NFKC", message or "")
    text = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    text = text.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
    if lines and re.match(r"^(?:from|sender)\s*[:：]", lines[0], flags=re.IGNORECASE):
        lines = lines[1:]
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def sms_fingerprint(
    message: str,
    merchant_id: int | None = None,
    bank_code: str | None = None,
    amount_rial: int | None = None,
) -> str:
    """Canonical idempotency key for an incoming transaction message.

    Sender and device id are intentionally excluded: Android can expose the same
    bank sender with different labels for SMS/RCS, and two forwarding filters can
    submit the same message.  Merchant, normalized bank, exact amount and the
    normalized message body are sufficient to identify the same delivery.
    """
    raw = "\n".join(
        [
            str(merchant_id or 0),
            normalize_bank_code(bank_code or "generic"),
            str(amount_rial or 0),
            _normalize_sms_text(message),
        ]
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def legacy_sms_fingerprint(sender: str, message: str, device_id: str | None, merchant_id: int | None = None) -> str:
    """Fingerprint used by versions <= 0.3.4, kept for seamless retries."""
    raw = f"{merchant_id or 0}\n{sender}\n{device_id or ''}\n{message.strip()}"
    return sha256(raw.encode("utf-8")).hexdigest()


def normalize_source_id(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())


_CREDIT_INTENT_WORDS = (
    "واریز", "واريز", "بستانکار", "بستانكار", "افزایش موجودی", "افزايش موجودي",
    "به حساب شما نشست", "به حسابتان نشست", "به حساب شما واریز شد", "به حساب شما واريز شد",
    "دریافت وجه", "دريافت وجه", "وصول", "deposit", "credited", "credit",
)

_NON_PAYMENT_WORDS = (
    "رمز پویا", "رمز پويا", "رمز دوم", "otp", "کد تایید", "کد تأیید",
    "برداشت", "بدهکار", "بدهكار", "خرید", "خريد", "کسر", "كسر",
    "پاسخ دادیم", "تیکت", "تيکت", "سفارش جدید", "پنل کاربری", "لغو",
)


def _normalized_for_screening(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    text = text.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    return re.sub(r"\s+", " ", text).strip().casefold()


def should_surface_unconfirmed_sms(message: str, bank_code: str | None, amount_rial: int | None) -> bool:
    """Return True only for messages that genuinely resemble an incoming payment."""
    text = _normalized_for_screening(message)
    if any(word.casefold() in text for word in _NON_PAYMENT_WORDS):
        return False

    has_credit_intent = any(word.casefold() in text for word in _CREDIT_INTENT_WORDS)
    has_currency_amount = bool(
        amount_rial
        or re.search(r"\d[\d, .]{2,}\s*(?:ریال|ريال|تومان)", text, flags=re.IGNORECASE)
    )
    known_bank = normalize_bank_code(bank_code or "generic") != "generic"
    return has_credit_intent and (has_currency_amount or known_bank)




async def _dynamic_template_parse(
    session: AsyncSession,
    *,
    merchant_id: int | None,
    sender: str,
    message: str,
) -> ParsedSms | None:
    templates = list((await session.scalars(
        select(SmsParserTemplate).where(
            SmsParserTemplate.is_active.is_(True),
            *([SmsParserTemplate.merchant_id.in_([None, merchant_id])] if merchant_id is not None else [SmsParserTemplate.merchant_id.is_(None)]),
        ).order_by(SmsParserTemplate.merchant_id.desc(), SmsParserTemplate.confidence.desc(), SmsParserTemplate.id.asc())
    )).all())
    text = normalize_text(message)
    for template in templates:
        try:
            if template.sender_pattern and not re.search(template.sender_pattern[:500], sender, flags=re.IGNORECASE):
                continue
            if template.credit_pattern and not re.search(template.credit_pattern[:500], text, flags=re.IGNORECASE):
                continue
            amount_match = re.search(template.amount_pattern[:500], text, flags=re.IGNORECASE)
            if not amount_match:
                continue
            raw_amount = amount_match.group(1) if amount_match.groups() else amount_match.group(0)
            amount_digits = digits_only(raw_amount)
            if not amount_digits:
                continue
            amount_rial = int(amount_digits)
            unit_text = amount_match.group(2) if len(amount_match.groups()) >= 2 else ""
            if "تومان" in (unit_text or ""):
                amount_rial *= 10
            card_last4 = None
            if template.card_pattern:
                card_match = re.search(template.card_pattern[:500], text, flags=re.IGNORECASE)
                if card_match:
                    card_last4 = digits_only(card_match.group(1) if card_match.groups() else card_match.group(0))[-4:] or None
            reference = None
            if template.reference_pattern:
                ref_match = re.search(template.reference_pattern[:500], text, flags=re.IGNORECASE)
                if ref_match:
                    reference = (ref_match.group(1) if ref_match.groups() else ref_match.group(0))[:120]
            return ParsedSms(
                bank_code=template.bank_code,
                is_credit=True,
                amount_rial=amount_rial,
                card_last4=card_last4,
                reference_number=reference,
                transaction_at=datetime.now(timezone.utc),
                confidence=max(65, min(100, template.confidence)),
                reason=f"dynamic_template:{template.id}",
            )
        except (re.error, ValueError, IndexError):
            continue
    return None


@dataclass(slots=True)
class SmsIngestDiagnostic:
    result: str
    detail: str
    amount_candidate_count: int = 0
    bank_candidate_count: int = 0
    source_candidate_count: int = 0
    notify: bool = True


async def ingest_sms(
    session: AsyncSession,
    sender: str,
    message: str,
    device_id: str | None,
    merchant_id: int | None = None,
    bank_hint: str | None = None,
) -> tuple[SmsTransaction, Invoice | None, SmsIngestDiagnostic]:
    parsed = parse_bank_sms(sender, message, bank_hint=bank_hint)
    dynamic = await _dynamic_template_parse(
        session, merchant_id=merchant_id, sender=sender, message=message
    )
    if dynamic and (not parsed.is_credit or not parsed.amount_rial or dynamic.confidence > parsed.confidence):
        parsed = dynamic
    fingerprint = sms_fingerprint(
        message,
        merchant_id=merchant_id,
        bank_code=parsed.bank_code,
        amount_rial=parsed.amount_rial,
    )
    old_fingerprint = legacy_sms_fingerprint(sender, message, device_id, merchant_id)
    existing = await session.scalar(
        select(SmsTransaction).where(SmsTransaction.fingerprint.in_([fingerprint, old_fingerprint]))
    )

    # Compatibility fallback: a duplicate can arrive with another sender label
    # (for example SMS vs RCS). Rows written by <=0.3.4 used sender in their
    # fingerprint, so compare recent matched messages by canonical content too.
    if not existing and merchant_id is not None and parsed.amount_rial:
        recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        recent = list(
            (
                await session.scalars(
                    select(SmsTransaction)
                    .join(Invoice, SmsTransaction.matched_invoice_id == Invoice.id)
                    .where(
                        Invoice.merchant_id == merchant_id,
                        SmsTransaction.status == "matched",
                        SmsTransaction.amount_rial == parsed.amount_rial,
                        SmsTransaction.created_at >= recent_cutoff,
                    )
                    .order_by(SmsTransaction.id.desc())
                    .limit(30)
                )
            ).all()
        )
        for candidate in recent:
            candidate_key = sms_fingerprint(
                candidate.raw_message,
                merchant_id=merchant_id,
                bank_code=candidate.bank_code,
                amount_rial=candidate.amount_rial,
            )
            if candidate_key == fingerprint:
                existing = candidate
                break

    previous_status = existing.status if existing else None
    if existing and (existing.status == "matched" or existing.matched_invoice_id):
        return existing, None, SmsIngestDiagnostic(
            "duplicate",
            "این تراکنش قبلاً تأیید شده است؛ ارسال تکراری بدون اعلان و بدون کسر مجدد نادیده گرفته شد",
            notify=False,
        )
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
        sms.fingerprint = fingerprint
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
            return existing, None, SmsIngestDiagnostic("duplicate", "اثر انگشت پیامک قبلاً ثبت شده است", notify=False)

    if not parsed.is_credit:
        if should_surface_unconfirmed_sms(message, parsed.bank_code, parsed.amount_rial):
            sms.status = "review"
            return sms, None, SmsIngestDiagnostic(
                "not_credit",
                "پیامک شبیه واریز است اما نشانه‌های کافی برای تأیید قطعی ندارد",
                notify=previous_status != "review",
            )

        sms.status = "ignored"
        return sms, None, SmsIngestDiagnostic(
            "ignored_non_payment",
            "پیامک غیرپرداختی شناسایی و بی‌صدا نادیده گرفته شد",
            notify=False,
        )
    if not parsed.amount_rial:
        sms.status = "review"
        return sms, None, SmsIngestDiagnostic("amount_not_found", "مبلغ واریز از متن پیامک استخراج نشد", notify=previous_status != "review")
    if parsed.confidence < 65:
        sms.status = "review"
        return sms, None, SmsIngestDiagnostic(
            "low_confidence", f"اطمینان Parser پایین است: {parsed.confidence}%", notify=previous_status != "review"
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
            notify=previous_status not in {"unmatched", "review"},
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
            notify=previous_status != "unmatched",
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
                notify=previous_status != "review",
            )

    if len(source_candidates) != 1:
        sms.status = "review"
        return sms, None, SmsIngestDiagnostic(
            "ambiguous",
            f"{len(source_candidates)} فاکتور هم‌مبلغ و هم‌بانک پیدا شد؛ تأیید خودکار امن نیست",
            amount_candidate_count=len(amount_candidates),
            bank_candidate_count=len(bank_candidates),
            source_candidate_count=len(source_candidates),
            notify=previous_status != "review",
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
            notify=previous_status != "review",
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
