from sqlalchemy import create_engine, inspect

from app.db.base import Base
from app.services.invoice_service import (
    WALLET_TOPUP_MAX_RIAL,
    WALLET_TOPUP_MIN_RIAL,
    calculate_customer_fee,
)


def test_zero_fee_is_free_in_all_split_modes():
    assert calculate_customer_fee(0, "customer") == 0
    assert calculate_customer_fee(0, "split") == 0
    assert calculate_customer_fee(0, "merchant") == 0


def test_split_fee_rounds_to_half_rial_amount():
    assert calculate_customer_fee(20_000, "split") == 10_000


def test_wallet_topup_limits_are_sane():
    assert WALLET_TOPUP_MIN_RIAL == 100_000
    assert WALLET_TOPUP_MAX_RIAL == 1_000_000_000


def test_invoice_schema_contains_internal_topup_fields():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    columns = {item["name"] for item in inspect(engine).get_columns("invoices")}
    assert {"purpose", "wallet_target_merchant_id"}.issubset(columns)
