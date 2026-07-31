from app.services.settings_service import (
    DEFAULT_FEE_MODE,
    DEFAULT_VERIFICATION_FEE_RIAL,
    normalize_default_fee_mode,
    normalize_default_fee_rial,
)


def test_default_fee_amount_accepts_zero_for_free_service():
    assert normalize_default_fee_rial("0") == 0


def test_default_fee_amount_rejects_negative_values():
    assert normalize_default_fee_rial("-500") == 0


def test_default_fee_amount_falls_back_on_invalid_value():
    assert normalize_default_fee_rial("invalid") == DEFAULT_VERIFICATION_FEE_RIAL


def test_default_fee_mode_falls_back_to_merchant():
    assert normalize_default_fee_mode("unknown") == DEFAULT_FEE_MODE
    assert normalize_default_fee_mode("split") == "split"
