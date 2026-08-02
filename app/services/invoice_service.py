from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.models import AmountReservation, BankCard, Invoice, Merchant, WalletLedger
from app.services.callback_outbox_service import enqueue_live_paid_callback
from app.services.timeline_service import record_payment_event

VALID_FEE_MODES = {"customer", "split", "merchant"}
API_FEE_MODES = VALID_FEE_MODES | {"default"}
UNIQUE_AMOUNT_MIN_TOMAN = 1
UNIQUE_AMOUNT_MAX_TOMAN = 999
WALLET_TOPUP_MIN_RIAL = 100_000  # 10,000 toman
WALLET_TOPUP_MAX_RIAL = 1_000_000_000  # 100,000,000 toman


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
    client_order_id: str | None = None,
    fee_mode: str | None = None,
    card_id: int | None = None,
    callback_url: str | None = None,
    return_url: str | None = None,
    callback_secret: str | None = None,
    ttl_minutes: int | None = None,
    store_id: int | None = None,
    api_key_id: int | None = None,
    idempotency_key: str | None = None,
    risk_score: int = 0,
    risk_status: str = "approved",
    source_channel: str | None = None,
) -> Invoice:
    if base_amount_rial < 10_000:
        raise ValueError("مبلغ فاکتور باید حداقل ۱٬۰۰۰ تومان باشد")
    fee_mode = merchant.fee_mode if fee_mode in {None, "default"} else fee_mode
    if fee_mode not in VALID_FEE_MODES:
        raise ValueError("حالت کارمزد نامعتبر است؛ مقادیر مجاز customer، split، merchant و default هستند")

    locked = await session.scalar(select(Merchant).where(Merchant.id == merchant.id).with_for_update())
    if not locked or not locked.is_active:
        raise ValueError("حساب پذیرنده فعال نیست")

    fee = max(0, locked.verification_fee_rial)
    if fee > 0 and locked.available_balance_rial < fee:
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

    if card.daily_limit_rial:
        start_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        card_volume_today = int(
            await session.scalar(
                select(func.coalesce(func.sum(Invoice.payable_amount_rial), 0)).where(
                    Invoice.card_id == card.id,
                    Invoice.created_at >= start_day,
                    Invoice.status.in_(["pending", "paid"]),
                )
            )
            or 0
        )
        if card_volume_today + payable_amount_rial > card.daily_limit_rial:
            raise ValueError("سقف مبلغ روزانه کارت مقصد تکمیل شده است؛ کارت دیگری را انتخاب کنید")

    invoice = Invoice(
        token=token,
        merchant_id=locked.id,
        card_id=card.id,
        order_id=order_id[:120],
        client_order_id=client_order_id[:120] if client_order_id else None,
        description=description,
        idempotency_key=idempotency_key[:180] if idempotency_key else None,
        base_amount_rial=base_amount_rial,
        fee_amount_rial=fee,
        customer_fee_rial=customer_fee,
        unique_amount_rial=unique_amount_rial,
        payable_amount_rial=payable_amount_rial,
        fee_mode=fee_mode,
        environment="live",
        risk_score=max(0, min(100, risk_score)),
        risk_status=risk_status,
        received_amount_rial=0,
        completion_mode="exact",
        source_channel=(source_channel or None),
        status="pending",
        expires_at=expires_at,
        # Store invoices use only the callback configured for that store (or
        # an explicit per-invoice override). They never inherit another store
        # or the old merchant-wide callback. Legacy/manual invoices keep the
        # merchant-level fallback for backward compatibility.
        callback_url=(callback_url if store_id is not None else callback_url or locked.callback_url),
        return_url=return_url,
        callback_secret=(callback_secret if store_id is not None else callback_secret or locked.callback_secret),
        store_id=store_id,
        api_key_id=api_key_id,
    )
    session.add(invoice)
    await session.flush()
    await record_payment_event(
        session,
        invoice,
        "invoice.created",
        detail={
            "order_id": invoice.client_order_id or invoice.order_id,
            "payable_amount_rial": invoice.payable_amount_rial,
            "environment": invoice.environment,
            "risk_score": invoice.risk_score,
            "risk_status": invoice.risk_status,
        },
    )
    if fee > 0:
        balance_before = locked.wallet_balance_rial
        reserved_before = locked.reserved_balance_rial
        locked.reserved_balance_rial += fee
        session.add(
            WalletLedger(
                merchant_id=locked.id,
                invoice_id=invoice.id,
                entry_type="fee_reserved",
                amount_rial=-fee,
                balance_before_rial=balance_before,
                balance_after_rial=locked.wallet_balance_rial,
                reserved_before_rial=reserved_before,
                reserved_after_rial=locked.reserved_balance_rial,
                reference_type="invoice",
                reference_id=str(invoice.id),
                description=f"رزرو کارمزد فاکتور {invoice.order_id}",
                idempotency_key=f"reserve:{invoice.id}",
            )
        )
        await session.flush()
    return invoice


async def create_wallet_topup_invoice(
    session: AsyncSession,
    target_merchant: Merchant,
    amount_rial: int,
    ttl_minutes: int | None = None,
) -> Invoice:
    """Create a fee-free internal invoice that charges the platform card.

    The payment is collected on an active card owned by the primary admin. After
    bank-SMS confirmation, the exact paid amount (including the matching suffix)
    is credited to the target merchant wallet, so no part of the payment is lost.
    """
    if amount_rial < WALLET_TOPUP_MIN_RIAL:
        raise ValueError("حداقل مبلغ شارژ کیف پول ۱۰٬۰۰۰ تومان است")
    if amount_rial > WALLET_TOPUP_MAX_RIAL:
        raise ValueError("حداکثر مبلغ شارژ کیف پول ۱۰۰٬۰۰۰٬۰۰۰ تومان است")

    target = await session.scalar(select(Merchant).where(Merchant.id == target_merchant.id).with_for_update())
    if not target or not target.is_active:
        raise ValueError("حساب پذیرنده فعال نیست")

    collection_owner = await session.scalar(
        select(Merchant)
        .join(BankCard, BankCard.merchant_id == Merchant.id)
        .where(
            Merchant.is_admin.is_(True),
            Merchant.is_active.is_(True),
            BankCard.is_active.is_(True),
        )
        .order_by(Merchant.id.asc(), BankCard.is_default.desc(), BankCard.priority.asc(), BankCard.id.asc())
        .limit(1)
    )
    if not collection_owner:
        raise ValueError("روش شارژ فعال نیست؛ مدیر باید یک کارت مقصد فعال در حساب مدیریتی ثبت کند")

    card = await choose_card(session, collection_owner.id)
    token = secrets.token_urlsafe(18)
    expires_at = utcnow() + timedelta(minutes=ttl_minutes or settings.invoice_ttl_minutes)
    unique_amount_rial, payable_amount_rial = await reserve_unique_payable_amount(
        session,
        card_id=card.id,
        nominal_amount_rial=amount_rial,
        invoice_token=token,
        expires_at=expires_at,
    )
    invoice = Invoice(
        token=token,
        merchant_id=collection_owner.id,
        card_id=card.id,
        order_id=f"TOPUP-{target.id}-{secrets.token_hex(5).upper()}",
        description=f"شارژ کیف پول BP-{target.id:06d}",
        base_amount_rial=amount_rial,
        fee_amount_rial=0,
        customer_fee_rial=0,
        unique_amount_rial=unique_amount_rial,
        payable_amount_rial=payable_amount_rial,
        fee_mode="merchant",
        purpose="wallet_topup",
        wallet_target_merchant_id=target.id,
        status="pending",
        expires_at=expires_at,
        callback_url=None,
        callback_secret=None,
    )
    session.add(invoice)
    await session.flush()
    await record_payment_event(
        session,
        invoice,
        "invoice.created",
        detail={"purpose": "wallet_topup", "payable_amount_rial": invoice.payable_amount_rial},
    )
    return invoice


async def release_invoice_reservation(session: AsyncSession, invoice: Invoice, status: str) -> bool:
    if invoice.status not in {"pending", "partially_paid"}:
        return False
    merchant = await session.scalar(select(Merchant).where(Merchant.id == invoice.merchant_id).with_for_update())
    if not merchant:
        return False
    fee = max(0, invoice.fee_amount_rial)
    balance_before = merchant.wallet_balance_rial
    reserved_before = merchant.reserved_balance_rial
    if fee > 0:
        merchant.reserved_balance_rial = max(0, merchant.reserved_balance_rial - fee)
    invoice.status = status
    await session.execute(
        delete(AmountReservation).where(AmountReservation.invoice_token == invoice.token)
    )
    if fee > 0:
        session.add(
            WalletLedger(
                merchant_id=merchant.id,
                invoice_id=invoice.id,
                entry_type="fee_released",
                amount_rial=fee,
                balance_before_rial=balance_before,
                balance_after_rial=merchant.wallet_balance_rial,
                reserved_before_rial=reserved_before,
                reserved_after_rial=merchant.reserved_balance_rial,
                reference_type="invoice",
                reference_id=str(invoice.id),
                description=f"آزادسازی کارمزد فاکتور {invoice.order_id}",
                idempotency_key=f"release:{invoice.id}",
            )
        )
    await record_payment_event(
        session,
        invoice,
        f"invoice.{status}",
        status=status,
        detail={"released_fee_rial": fee},
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
        .options(joinedload(Invoice.card), joinedload(Invoice.merchant), joinedload(Invoice.store))
        .with_for_update()
    )
    if not invoice or invoice.status not in {"pending", "partially_paid"} or invoice.matched_sms_id:
        return None

    merchant = await session.scalar(select(Merchant).where(Merchant.id == invoice.merchant_id).with_for_update())
    if not merchant:
        return None

    wallet_target = None
    if invoice.purpose == "wallet_topup":
        if not invoice.wallet_target_merchant_id:
            return None
        wallet_target = await session.scalar(
            select(Merchant).where(Merchant.id == invoice.wallet_target_merchant_id).with_for_update()
        )
        if not wallet_target:
            return None

    fee = max(0, invoice.fee_amount_rial)
    merchant_balance_before = merchant.wallet_balance_rial
    merchant_reserved_before = merchant.reserved_balance_rial
    if fee > 0:
        merchant.reserved_balance_rial = max(0, merchant.reserved_balance_rial - fee)
        merchant.wallet_balance_rial -= fee
    invoice.status = "paid"
    invoice.received_amount_rial = max(int(invoice.received_amount_rial or 0), invoice.payable_amount_rial)
    invoice.paid_at = utcnow()
    invoice.matched_sms_id = sms_id
    invoice.reference_number = reference_number
    if invoice.purpose == "payment":
        invoice.callback_status = "queued" if invoice.callback_url and invoice.callback_secret else "not_configured"
        invoice.callback_last_result = None
        invoice.callback_attempted_at = None
    else:
        invoice.callback_status = "skipped"
    await session.execute(
        delete(AmountReservation).where(AmountReservation.invoice_token == invoice.token)
    )

    if fee > 0:
        session.add(
            WalletLedger(
                merchant_id=merchant.id,
                invoice_id=invoice.id,
                entry_type="verification_fee",
                amount_rial=-fee,
                balance_before_rial=merchant_balance_before,
                balance_after_rial=merchant.wallet_balance_rial,
                reserved_before_rial=merchant_reserved_before,
                reserved_after_rial=merchant.reserved_balance_rial,
                reference_type="invoice",
                reference_id=str(invoice.id),
                description=f"کسر کارمزد تأیید فاکتور {invoice.order_id}",
                idempotency_key=f"paid-fee:{invoice.id}",
            )
        )

    if wallet_target is not None:
        credited_rial = invoice.payable_amount_rial
        target_balance_before = wallet_target.wallet_balance_rial
        target_reserved_before = wallet_target.reserved_balance_rial
        wallet_target.wallet_balance_rial += credited_rial
        session.add(
            WalletLedger(
                merchant_id=wallet_target.id,
                invoice_id=invoice.id,
                entry_type="wallet_topup",
                amount_rial=credited_rial,
                balance_before_rial=target_balance_before,
                balance_after_rial=wallet_target.wallet_balance_rial,
                reserved_before_rial=target_reserved_before,
                reserved_after_rial=wallet_target.reserved_balance_rial,
                reference_type="invoice",
                reference_id=str(invoice.id),
                description=f"شارژ آنلاین کیف پول با فاکتور {invoice.order_id}",
                idempotency_key=f"wallet-topup:{invoice.id}",
            )
        )

    await record_payment_event(
        session,
        invoice,
        "invoice.paid",
        status="paid",
        actor_type="bank_sms",
        actor_id=sms_id,
        detail={"reference_number": reference_number, "purpose": invoice.purpose},
    )
    await session.flush()
    if invoice.purpose == "payment":
        await enqueue_live_paid_callback(session, invoice)
    await session.flush()
    return invoice


async def get_invoice_by_token(session: AsyncSession, token: str) -> Invoice | None:
    return await session.scalar(
        select(Invoice)
        .where(Invoice.token == token)
        .options(joinedload(Invoice.card), joinedload(Invoice.merchant), joinedload(Invoice.store))
    )
