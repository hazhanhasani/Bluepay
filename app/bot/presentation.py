from __future__ import annotations

import html
from collections.abc import Iterable

BRAND_NAME = "بلوپی"
BRAND_EN = "BluePay"
DIVIDER = "━━━━━━━━━━━━━━━━"
SUBDIVIDER = "──────────────"

FEE_MODE_LABELS = {
    "customer": "پرداخت کامل توسط مشتری",
    "split": "تقسیم مساوی بین مشتری و پذیرنده",
    "merchant": "پرداخت کامل توسط پذیرنده",
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

STATUS_BADGES = {
    "active": "🟢 فعال",
    "inactive": "🔴 غیرفعال",
    "ready": "🟢 آماده",
    "configured": "🟢 متصل",
    "missing": "🟡 تنظیم نشده",
    "pending": "🟠 در انتظار",
    "paid": "🟢 پرداخت‌شده",
    "failed": "🔴 ناموفق",
}


def esc(value: object | None) -> str:
    return html.escape(str(value if value is not None else "-"))


def money_toman(value_rial: int | None) -> str:
    return f"{(value_rial or 0) // 10:,} تومان"


def fee_mode_label(value: str | None) -> str:
    return FEE_MODE_LABELS.get(value or "", value or "نامشخص")


def invoice_status_label(value: str | None) -> str:
    return INVOICE_STATUS_LABELS.get(value or "", value or "نامشخص")


def sms_result_label(value: str | None) -> str:
    return SMS_RESULT_LABELS.get(value or "", value or "نامشخص")


def badge(value: str) -> str:
    return STATUS_BADGES.get(value, value)


def page_title(icon: str, title: str, subtitle: str | None = None) -> str:
    text = f"{icon} <b>{esc(title)}</b>"
    if subtitle:
        text += f"\n<i>{esc(subtitle)}</i>"
    return text


def field(label: str, value: object, *, icon: str = "•", code: bool = False, bold: bool = True) -> str:
    safe = esc(value)
    if code:
        rendered = f"<code>{safe}</code>"
    elif bold:
        rendered = f"<b>{safe}</b>"
    else:
        rendered = safe
    return f"{icon} {esc(label)}: {rendered}"


def progress(step: int, total: int, title: str) -> str:
    filled = "●" * max(0, min(step, total))
    empty = "○" * max(0, total - step)
    return f"<b>{esc(title)}</b>\n<code>{filled}{empty}</code>  مرحله {step} از {total}"


def panel(
    icon: str,
    title: str,
    lines: Iterable[str] | str,
    *,
    subtitle: str | None = None,
    footer: str | None = None,
) -> str:
    if isinstance(lines, str):
        body = lines.strip()
    else:
        body = "\n".join(str(line) for line in lines if str(line).strip())
    parts = [page_title(icon, title, subtitle), DIVIDER]
    if body:
        parts.append(body)
    if footer:
        parts.extend([DIVIDER, footer.strip()])
    return "\n".join(parts)


def success(title: str, detail: str, *, footer: str | None = None) -> str:
    return panel("✅", title, detail, footer=footer)


def warning(title: str, detail: str, *, footer: str | None = None) -> str:
    return panel("⚠️", title, detail, footer=footer)


def error(title: str, detail: str, *, footer: str | None = None) -> str:
    return panel("❌", title, detail, footer=footer)


def info(title: str, detail: str, *, footer: str | None = None) -> str:
    return panel("ℹ️", title, detail, footer=footer)
