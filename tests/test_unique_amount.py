from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models import BankCard, Merchant
from app.services.invoice_service import create_invoice


@pytest.mark.asyncio
async def test_same_base_amount_gets_distinct_payable_amounts(tmp_path: Path):
    db_path = tmp_path / "unique.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        merchant = Merchant(telegram_user_id=123, wallet_balance_rial=10_000_000, verification_fee_rial=20_000)
        session.add(merchant)
        await session.flush()
        card = BankCard(
            merchant_id=merchant.id, bank_code="blu", card_number_encrypted="x", card_last4="1476",
            account_holder="Test", is_active=True, is_default=True
        )
        session.add(card)
        await session.flush()
        first = await create_invoice(session, merchant, 500_000, fee_mode="merchant", card_id=card.id)
        second = await create_invoice(session, merchant, 500_000, fee_mode="merchant", card_id=card.id)
        assert first.payable_amount_rial != second.payable_amount_rial
        assert 10 <= first.unique_amount_rial <= 9_990
        assert 10 <= second.unique_amount_rial <= 9_990
        await session.rollback()

    await engine.dispose()
