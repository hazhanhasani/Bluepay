from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class CreateInvoiceRequest(BaseModel):
    amount_toman: int = Field(
        ge=1_000,
        le=500_000_000,
        description="مبلغ پایه فاکتور به تومان؛ عدد صحیح از ۱٬۰۰۰ تا ۵۰۰٬۰۰۰٬۰۰۰",
        examples=[200_000],
    )
    order_id: str | None = Field(
        default=None,
        max_length=120,
        description="شناسه سفارش پذیرنده؛ در محدوده همان فروشگاه نباید تکراری باشد",
        examples=["ORDER-1001"],
    )
    description: str | None = Field(
        default=None,
        max_length=500,
        description="شرح قابل نمایش فاکتور",
        examples=["اشتراک یک‌ماهه"],
    )
    fee_mode: Literal["customer", "split", "merchant", "default"] | None = Field(
        default=None,
        description=(
            "نحوه تقسیم هزینه تأیید: customer تمام کارمزد را به مبلغ مشتری اضافه می‌کند؛ "
            "split نصف آن را اضافه می‌کند؛ merchant چیزی به مبلغ مشتری اضافه نمی‌کند؛ "
            "default یا حذف فیلد، تنظیم پیش‌فرض پذیرنده را اعمال می‌کند. مقدار free از API پذیرفته نمی‌شود؛ "
            "رایگان‌بودن سرویس فقط از تنظیم کارمزد صفر توسط مدیر تعیین می‌شود."
        ),
        examples=["split"],
    )
    card_id: int | None = Field(
        default=None,
        description="شناسه کارت مقصد فعال متعلق به همین پذیرنده؛ در صورت حذف، کارت پیش‌فرض انتخاب می‌شود",
    )
    callback_url: HttpUrl | None = Field(
        default=None,
        description="Callback اختصاصی همین فاکتور؛ باید HTTPS عمومی باشد",
    )
    ttl_minutes: int | None = Field(
        default=None,
        ge=5,
        le=1440,
        description="مهلت پرداخت بر حسب دقیقه؛ از ۵ تا ۱٬۴۴۰",
        examples=[30],
    )


class SmsWebhookRequest(BaseModel):
    sender: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=3, max_length=4000)
    device_id: str | None = Field(default=None, max_length=120)
    bank_code: str | None = Field(
        default=None,
        max_length=60,
        description="کد بانک اختیاری؛ ارسال آن دقت تشخیص پیامک را افزایش می‌دهد",
    )
