from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class CreateInvoiceRequest(BaseModel):
    amount_toman: int = Field(ge=1_000, le=500_000_000)
    order_id: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    fee_mode: str | None = Field(default=None, pattern="^(customer|split|merchant)$")
    card_id: int | None = None
    callback_url: HttpUrl | None = None
    ttl_minutes: int | None = Field(default=None, ge=5, le=1440)


class SmsWebhookRequest(BaseModel):
    sender: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=3, max_length=4000)
    device_id: str | None = Field(default=None, max_length=120)
    bank_code: str | None = Field(
        default=None,
        max_length=60,
        description="کد بانک اختیاری؛ ارسال آن دقت تشخیص پیامک را افزایش می‌دهد",
    )
