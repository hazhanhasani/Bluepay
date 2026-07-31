from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import api_key, random_secret, sha256_text
from app.models import Merchant, WalletLedger


async def get_or_create_merchant(session: AsyncSession, telegram_user_id: int, name: str) -> tuple[Merchant, bool]:
    merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == telegram_user_id))
    if merchant:
        return merchant, False

    count = await session.scalar(select(func.count(Merchant.id))) or 0
    merchant = Merchant(
        telegram_user_id=telegram_user_id,
        name=name[:120] or "پذیرنده",
        is_admin=(count == 0),
        callback_secret=random_secret(32),
    )
    session.add(merchant)
    await session.flush()
    return merchant, True


async def regenerate_api_key(session: AsyncSession, merchant: Merchant) -> str:
    plain = api_key()
    merchant.api_key_hash = sha256_text(plain)
    merchant.api_key_prefix = plain[:12]
    await session.flush()
    return plain


async def credit_wallet(
    session: AsyncSession,
    merchant: Merchant,
    amount_rial: int,
    description: str,
    idempotency_key: str,
) -> None:
    if amount_rial <= 0:
        raise ValueError("مبلغ شارژ باید بیشتر از صفر باشد")
    merchant.wallet_balance_rial += amount_rial
    session.add(
        WalletLedger(
            merchant_id=merchant.id,
            entry_type="deposit",
            amount_rial=amount_rial,
            balance_after_rial=merchant.wallet_balance_rial,
            description=description,
            idempotency_key=idempotency_key,
        )
    )
