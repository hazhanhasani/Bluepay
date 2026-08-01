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
    return_url: HttpUrl | None = Field(
        default=None,
        description="نشانی بازگشت کاربر پس از مشاهده رسید موفق؛ باید HTTPS عمومی باشد",
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


class SandboxCreateInvoiceRequest(BaseModel):
    amount_toman: int = Field(ge=1_000, le=500_000_000, examples=[200_000])
    order_id: str = Field(min_length=1, max_length=120, examples=["TEST-ORDER-1001"])
    description: str | None = Field(default=None, max_length=500)
    ttl_minutes: int = Field(default=30, ge=5, le=1440)


class SandboxSimulationRequest(BaseModel):
    result: Literal["paid", "failed", "expired"] = Field(
        description="نتیجه شبیه‌سازی؛ paid رویداد Callback آزمایشی ایجاد می‌کند"
    )
    reference_number: str | None = Field(default=None, max_length=120)


class StoreSecurityRequest(BaseModel):
    allowed_ips: list[str] = Field(default_factory=list, max_length=100)
    invoice_rate_limit_per_minute: int | None = Field(default=None, ge=1, le=300)
    daily_amount_limit_toman: int | None = Field(default=None, ge=1_000, le=5_000_000_000)


class SmsDevicePolicyRequest(BaseModel):
    devices: list[str] = Field(default_factory=list, max_length=50)

class SmsDeviceCreateRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=120)
    name: str | None = Field(default=None, max_length=120)
    allowed_bank_codes: list[str] = Field(default_factory=list, max_length=50)
    require_hmac: bool = True


class TeamMemberRequest(BaseModel):
    telegram_user_id: int = Field(gt=0)
    role: Literal["finance", "developer", "support", "viewer"]
