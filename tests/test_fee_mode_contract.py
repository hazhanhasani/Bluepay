import pytest
from pydantic import ValidationError

from app.api.schemas import CreateInvoiceRequest


def test_default_fee_mode_is_an_explicit_supported_alias():
    model = CreateInvoiceRequest(amount_toman=10_000, fee_mode="default")
    assert model.fee_mode == "default"


def test_free_fee_mode_cannot_bypass_admin_fee_configuration():
    with pytest.raises(ValidationError):
        CreateInvoiceRequest(amount_toman=10_000, fee_mode="free")
