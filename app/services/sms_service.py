from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import Invoice, SmsTransaction
from app.parsers import parse_bank_sms
from app.services.invoice_service import confirm_invoice_paid


def sms_fingerprint(sender: str, message: str, device_id: str | None) -> str:
    raw = f"{sender}\n{device_id or ''}\n{message.strip()}"
    return sha256(raw.encode("utf-8")).hexdigest()


async def ingest_sms(
    session: AsyncSession,
    sender: str,
    message: str,
    device_id: str | None,
) -> tuple[SmsTransaction, Invoice | None, str]:
    fingerprint = sms_fingerprint(sender, message, device_id)
    existing = await session.scalar(select(SmsTransaction).where(SmsTransaction.fingerprint == fingerprint))
    if existing:
        return existing, None, "duplicate"

    parsed = parse_bank_sms(sender, message)
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
        return existing, None, "duplicate"

    if not parsed.is_credit or not parsed.amount_rial or parsed.confidence < 65:
        sms.status = "review"
        return sms, None, "low_confidence"

    now = datetime.now(timezone.utc)
    candidates = list(
        (
            await session.scalars(
                select(Invoice)
                .where(
                    Invoice.status == "pending",
                    Invoice.payable_amount_rial == parsed.amount_rial,
                    Invoice.expires_at >= now,
                )
                .options(joinedload(Invoice.card), joinedload(Invoice.merchant))
            )
        ).all()
    )

    filtered: list[Invoice] = []
    for invoice in candidates:
        card = invoice.card
        if parsed.bank_code != "generic" and card.bank_code != parsed.bank_code:
            continue
        if parsed.card_last4 and card.card_last4 != parsed.card_last4:
            continue
        if device_id and card.sms_source_id and card.sms_source_id != device_id:
            continue
        filtered.append(invoice)

    if not filtered:
        sms.status = "unmatched"
        return sms, None, "unmatched"

    # انتخاب بر اساس «جدیدتر بودن» می‌تواند فاکتور اشتباه را تأیید کند.
    # فقط وقتی یک کاندیدای قطعی وجود دارد، تأیید خودکار انجام می‌شود.
    if len(filtered) != 1:
        sms.status = "review"
        return sms, None, "ambiguous"

    invoice = await confirm_invoice_paid(session, filtered[0].id, sms.id, parsed.reference_number)
    if not invoice:
        sms.status = "review"
        return sms, None, "race_or_already_paid"

    sms.status = "matched"
    sms.matched_invoice_id = invoice.id
    return sms, invoice, "matched"
