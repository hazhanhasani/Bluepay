from __future__ import annotations

DIVIDER = "━━━━━━━━━━━━━━━━"

FEE_MODE_LABELS = {
    "customer": "تمام کارمزد بر عهده مشتری",
    "split": "تقسیم مساوی بین مشتری و پذیرنده",
    "merchant": "تمام کارمزد بر عهده پذیرنده",
}

INVOICE_STATUS_LABELS = {
    "pending": "در انتظار پرداخت",
    "paid": "پرداخت‌شده",
    "expired": "منقضی‌شده",
    "cancelled": "لغوشده",
    "review": "نیازمند بررسی",
    "failed": "ناموفق",
}

SMS_RESULT_LABELS = {
    "matched": "تطبیق و تأیید شد",
    "duplicate": "پیامک تکراری",
    "not_credit": "واریز قطعی تشخیص داده نشد",
    "no_amount_candidate": "فاکتور فعال با مبلغ دقیق یافت نشد",
    "ambiguous_amount": "چند فاکتور هم‌مبلغ یافت شد",
    "bank_mismatch": "بانک پیامک با کارت مقصد مطابقت ندارد",
    "card_mismatch": "کارت پیامک با کارت مقصد مطابقت ندارد",
    "source_mismatch": "منبع پیامک با کارت مقصد مطابقت ندارد",
    "review": "برای بررسی دستی ثبت شد",
    "unmatched": "فاکتور منطبق یافت نشد",
}


def fee_mode_label(value: str | None) -> str:
    return FEE_MODE_LABELS.get(value or "", value or "نامشخص")


def invoice_status_label(value: str | None) -> str:
    return INVOICE_STATUS_LABELS.get(value or "", value or "نامشخص")


def sms_result_label(value: str | None) -> str:
    return SMS_RESULT_LABELS.get(value or "", value or "نامشخص")
