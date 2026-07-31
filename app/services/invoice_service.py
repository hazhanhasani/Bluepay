from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.models import AmountReservation, BankCard, Invoice, Merchant, WalletLedger

VALID_FEE_MODES = {"customer", "split", "merchant"}
UNIQUE_AMOUNT_MIN_TOMAN = 1
UNIQUE_AMOUNT_MAX_TOMAN = 999


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def calculate_customer_fee(fee_rial: int, fee_mode: str) -> int:
    if fee_mode == "customer":
        return fee_rial
    if fee_mode == "split":
        return fee_rial // 2
    return 0


async def reserve_unique_payable_amount(
    session: AsyncSession,
    *,
    card_id: int,
    nominal_amount_rial: int,
    invoice_token: str,
    expires_at: datetime,
) -> tuple[int, int]:
    """Reserve an exact payable amount for one pending invoice.

    A 1..999 toman matching code is added to the nominal amount. The separate
    reservation table has a database unique constraint, so even simultaneous
    invoices cannot receive the same final amount on the same destination card.
    """
    now = utcnow()
    await session.execute(delete(AmountReservation).where(AmountReservation.expires_at < now))

    span = UNIQUE_AMOUNT_MAX_TOMAN - UNIQUE_AMOUNT_MIN_TOMAN + 1
    start = secrets.randbelow(span) + UNIQUE_AMOUNT_MIN_TOMAN
    for offset in range(span):
        code_toman = UNIQUE_AMOUNT_MIN_TOMAN + ((start - UNIQUE_AMOUNT_MIN_TOMAN + offset) % span)
        unique_amount_rial = code_toman * 10
        payable_amount_rial = nominal_amount_rial + unique_amount_rial
        reservation = AmountReservation(
            card_id=card_id,
            invoice_token=invoice_token,
            payable_amount_rial=payable_amount_rial,
            expires_at=expires_at,
        )
        try:
            async with session.begin_nested():
                session.add(reservation)
                await session.flush()
            return unique_amount_rial, payable_amount_rial
        except IntegrityError:
            # Another active invoice already owns this exact amount on the card.
            continue

    raise ValueError("ظرفیت مبلغ‌های یکتا برای این کارت تکمیل است؛ چند دقیقه بعد دوباره تلاش کنید")


async def choose_card(session: AsyncSession, merchant_id: int, card_id: int | None = None) -> BankCard:
    stmt = select(BankCard).where(BankCard.merchant_id == merchant_id, BankCard.is_active.is_(True))
    if card_id:
        stmt = stmt.where(BankCard.id == card_id)
    else:
        stmt = stmt.order_by(BankCard.is_default.desc(), BankCard.priority.asc(), BankCard.id.asc())
    card = await session.scalar(stmt.limit(1))
    if not card:
        raise ValueError("هیچ کارت بانکی فعالی برای پذیرنده ثبت نشده است")
    return card


async def create_invoice(
    session: AsyncSession,
    merchant: Merchant,
    base_amount_rial: int,
    description: str | None = None,
    order_id: str | None = None,
    fee_mode: str | None = None,
    card_id: int | None = None,
    callback_url: str | None = None,
    ttl_minutes: int | None = None,
) -> Invoice:
    if base_amount_rial < 10_000:
        raise ValueError("مبلغ فاکتور باید حداقل ۱٬۰۰۰ تومان باشد")
    fee_mode = fee_mode or merchant.fee_mode
    if fee_mode not in VALID_FEE_MODES:
        raise ValueError("حالت کارمزد نامعتبر است")

    locked = await session.scalar(select(Merchant).where(Merchant.id == merchant.id).with_for_update())
    if not locked or not locked.is_active:
        raise ValueError("حساب پذیرنده فعال نیست")

    fee = locked.verification_fee_rial
    if locked.available_balance_rial < fee:
        raise ValueError("موجودی قابل استفاده کیف پول برای رزرو کارمزد کافی نیست")

    card = await choose_card(session, locked.id, card_id)
    customer_fee = calculate_customer_fee(fee, fee_mode)
    token = secrets.token_urlsafe(18)
    order_id = order_id or f"MANUAL-{secrets.token_hex(6).upper()}"
    expires_at = utcnow() + timedelta(minutes=ttl_minutes or settings.invoice_ttl_minutes)
    nominal_amount_rial = base_amount_rial + customer_fee
    unique_amount_rial, payable_amount_rial = await reserve_unique_payable_amount(
        session,
        card_id=card.id,
        nominal_amount_rial=nominal_amount_rial,
        invoice_token=token,
        expires_at=expires_at,
    )

    invoice = Invoice(
        token=token,
        merchant_id=locked.id,
        card_id=card.id,
        order_id=order_id[:120],
        description=description,
        base_amount_rial=base_amount_rial,
        fee_amount_rial=fee,
        customer_fee_rial=customer_fee,
        unique_amount_rial=unique_amount_rial,
        payable_amount_rial=payable_amount_rial,
        fee_mode=fee_mode,
        status="pending",
        expires_at=expires_at,
        callback_url=callback_url or locked.callback_url,
        callback_secret=locked.callback_secret,
    )
    session.add(invoice)
    locked.reserved_balance_rial += fee
    await session.flush()
    session.add(
        WalletLedger(
            merchant_id=locked.id,
            invoice_id=invoice.id,
            entry_type="fee_reserved",
            amount_rial=-fee,
            balance_after_rial=locked.wallet_balance_rial,
            description=f"رزرو کارمزد فاکتور {invoice.order_id}",
            idempotency_key=f"reserve:{invoice.id}",
        )
    )
    await session.flush()
    return invoice


async def release_invoice_reservation(session: AsyncSession, invoice: Invoice, status: str) -> bool:
    if invoice.status != "pending":
        return False
    merchant = await session.scalar(select(Merchant).where(Merchant.id == invoice.merchant_id).with_for_update())
    if not merchant:
        return False
    merchant.reserved_balance_rial = max(0, merchant.reserved_balance_rial - invoice.fee_amount_rial)
    invoice.status = status
    await session.execute(
        delete(AmountReservation).where(AmountReservation.invoice_token == invoice.token)
    )
    session.add(
        WalletLedger(
            merchant_id=merchant.id,
            invoice_id=invoice.id,
            entry_type="fee_released",
            amount_rial=invoice.fee_amount_rial,
            balance_after_rial=merchant.wallet_balance_rial,
            description=f"آزادسازی کارمزد فاکتور {invoice.order_id}",
            idempotency_key=f"release:{invoice.id}",
        )
    )
    return True


async def confirm_invoice_paid(
    session: AsyncSession,
    invoice_id: int,
    sms_id: int,
    reference_number: str | None,
) -> Invoice | None:
    invoice = await session.scalar(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(joinedload(Invoice.card), joinedload(Invoice.merchant))
        .with_for_update()
    )
    if not invoice or invoice.status != "pending" or invoice.matched_sms_id:
        return None

    merchant = await session.scalar(select(Merchant).where(Merchant.id == invoice.merchant_id).with_for_update())
    if not merchant:
        return None

    merchant.reserved_balance_rial = max(0, merchant.reserved_balance_rial - invoice.fee_amount_rial)
    merchant.wallet_balance_rial -= invoice.fee_amount_rial
    invoice.status = "paid"
    invoice.paid_at = utcnow()
    invoice.matched_sms_id = sms_id
    invoice.reference_number = reference_number
    await session.execute(
        delete(AmountReservation).where(AmountReservation.invoice_token == invoice.token)
    )

    session.add(
        WalletLedger(
            merchant_id=merchant.id,
            invoice_id=invoice.id,
            entry_type="verification_fee",
            amount_rial=-invoice.fee_amount_rial,
            balance_after_rial=merchant.wallet_balance_rial,
            description=f"کسر کارمزد تأیید فاکتور {invoice.order_id}",
            idempotency_key=f"paid-fee:{invoice.id}",
        )
    )
    await session.flush()
    return invoice


async def get_invoice_by_token(session: AsyncSession, token: str) -> Invoice | None:
    return await session.scalar(
        select(Invoice)
        .where(Invoice.token == token)
        .options(joinedload(Invoice.card), joinedload(Invoice.merchant))
    )
