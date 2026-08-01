from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from time import monotonic

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import BankCard, Invoice, Merchant, SmsTransaction, Store, StoreApiKey
from app.parsers import BANK_PROFILES, bank_label
from app.services.storage_service import storage
from app.version import APP_VERSION


_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_CACHE_LOCK = asyncio.Lock()
_CACHE_PAYLOAD: dict | None = None
_CACHE_AT = 0.0


def fa_number(value: int) -> str:
    return f"{int(value):,}".translate(_PERSIAN_DIGITS).replace(",", "٬")


def fa_digits(value: str | int) -> str:
    return str(value).translate(_PERSIAN_DIGITS)


def _status_payload(status: str) -> dict[str, str]:
    values = {
        "pending": {
            "label": "در انتظار پرداخت",
            "tone": "warning",
            "progress": "در انتظار پیامک واریز بانک…",
            "event_title": "فاکتور آماده پرداخت است",
            "event_subtitle": "مبلغ دقیق و کارت مقصد ثبت شده",
            "event_icon": "…",
        },
        "paid": {
            "label": "پرداخت تأیید شد",
            "tone": "success",
            "progress": "پیامک بانکی تطبیق و پرداخت تأیید شد",
            "event_title": "پرداخت تأیید شد",
            "event_subtitle": "تطبیق دقیق مبلغ، بانک و کارت",
            "event_icon": "✓",
        },
        "expired": {
            "label": "فاکتور منقضی شد",
            "tone": "muted",
            "progress": "مهلت پرداخت این فاکتور پایان یافته است",
            "event_title": "مهلت فاکتور پایان یافت",
            "event_subtitle": "برای پرداخت باید فاکتور جدید ساخته شود",
            "event_icon": "⌛",
        },
        "canceled": {
            "label": "فاکتور لغو شد",
            "tone": "danger",
            "progress": "این فاکتور دیگر قابل پرداخت نیست",
            "event_title": "فاکتور لغو شده است",
            "event_subtitle": "هیچ پرداختی برای آن تأیید نمی‌شود",
            "event_icon": "×",
        },
    }
    return values.get(
        status,
        {
            "label": "در حال پردازش",
            "tone": "info",
            "progress": "وضعیت تراکنش در حال بررسی است",
            "event_title": "تراکنش در حال بررسی است",
            "event_subtitle": "اطلاعات از سامانه بلوپی دریافت شد",
            "event_icon": "•",
        },
    )


def _callback_payload(invoice: Invoice | None) -> dict[str, str]:
    if invoice is None:
        return {
            "title": "Callback آماده اتصال",
            "subtitle": "پس از نخستین پرداخت، وضعیت اینجا نمایش داده می‌شود",
            "tone": "muted",
            "icon": "↗",
        }

    if invoice.status != "paid":
        if invoice.callback_url:
            return {
                "title": "Callback پس از پرداخت ارسال می‌شود",
                "subtitle": "مسیر اختصاصی فروشگاه تنظیم شده است",
                "tone": "info",
                "icon": "↗",
            }
        return {
            "title": "Callback تنظیم نشده",
            "subtitle": "فروشگاه می‌تواند مسیر مستقل خود را در ربات ثبت کند",
            "tone": "muted",
            "icon": "↗",
        }

    callback_status = (invoice.callback_status or "not_attempted").strip().lower()
    result = (invoice.callback_last_result or "").strip()
    if callback_status == "delivered":
        http_label = "پاسخ موفق فروشگاه"
        if result.startswith("http_"):
            parts = result.split("_")
            if len(parts) > 1 and parts[1].isdigit():
                http_label = f"HTTP {parts[1]} • تحویل موفق"
        return {
            "title": "Callback تحویل شد",
            "subtitle": http_label,
            "tone": "success",
            "icon": "↗",
        }
    if callback_status == "failed":
        return {
            "title": "تحویل Callback ناموفق بود",
            "subtitle": "سه تلاش انجام شد؛ فروشگاه باید مسیر خود را بررسی کند",
            "tone": "danger",
            "icon": "!",
        }
    if callback_status in {"queued", "sending"}:
        return {
            "title": "Callback در صف ارسال است",
            "subtitle": "نتیجه پرداخت به فروشگاه تحویل می‌شود",
            "tone": "info",
            "icon": "↗",
        }
    if callback_status in {"not_configured", "skipped"} or not invoice.callback_url:
        return {
            "title": "Callback تنظیم نشده",
            "subtitle": "پرداخت تأیید شد اما مقصد Callback وجود ندارد",
            "tone": "muted",
            "icon": "↗",
        }
    return {
        "title": "Callback در حال پردازش است",
        "subtitle": "وضعیت تحویل از سامانه دریافت می‌شود",
        "tone": "info",
        "icon": "↗",
    }


async def build_public_dashboard(session: AsyncSession, *, max_age_seconds: float = 5.0) -> dict:
    """Return public-safe live data sourced from the same database as the bot.

    No merchant identity, order id, API key, callback URL, phone number, token,
    reference number, or full card number is exposed by this payload. A short
    process-local cache prevents every public visitor from repeating aggregate
    SQLite queries while keeping the landing page close to real time.
    """
    global _CACHE_AT, _CACHE_PAYLOAD
    if _CACHE_PAYLOAD is not None and monotonic() - _CACHE_AT < max_age_seconds:
        return _CACHE_PAYLOAD

    async with _CACHE_LOCK:
        if _CACHE_PAYLOAD is not None and monotonic() - _CACHE_AT < max_age_seconds:
            return _CACHE_PAYLOAD

        payload = await _build_public_dashboard_uncached(session)
        _CACHE_PAYLOAD = payload
        _CACHE_AT = monotonic()
        return payload


async def _build_public_dashboard_uncached(session: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)

    latest_invoice = await session.scalar(
        select(Invoice)
        .where(Invoice.purpose == "payment")
        .options(joinedload(Invoice.card), joinedload(Invoice.store))
        .order_by(Invoice.created_at.desc(), Invoice.id.desc())
        .limit(1)
    )

    async def count(statement) -> int:
        return int((await session.scalar(statement)) or 0)

    active_merchants = await count(select(func.count(Merchant.id)).where(Merchant.is_active.is_(True)))
    active_stores = await count(select(func.count(Store.id)).where(Store.is_active.is_(True)))
    active_api_keys = await count(select(func.count(StoreApiKey.id)).where(StoreApiKey.is_active.is_(True)))
    active_cards = await count(select(func.count(BankCard.id)).where(BankCard.is_active.is_(True)))
    paid_invoices = await count(
        select(func.count(Invoice.id)).where(Invoice.purpose == "payment", Invoice.status == "paid")
    )
    paid_last_24h = await count(
        select(func.count(Invoice.id)).where(
            Invoice.purpose == "payment",
            Invoice.status == "paid",
            Invoice.paid_at.is_not(None),
            Invoice.paid_at >= since_24h,
        )
    )
    pending_invoices = await count(
        select(func.count(Invoice.id)).where(Invoice.purpose == "payment", Invoice.status == "pending")
    )
    matched_sms = await count(select(func.count(SmsTransaction.id)).where(SmsTransaction.status == "matched"))
    callback_stores = await count(
        select(func.count(Store.id)).where(
            Store.is_active.is_(True),
            Store.callback_url.is_not(None),
            Store.callback_url != "",
        )
    )

    backup = storage.status()
    operational = backup.get("last_error") is None

    if latest_invoice is None:
        invoice_data = {
            "exists": False,
            "status": "empty",
            "status_label": "در انتظار نخستین تراکنش",
            "status_tone": "muted",
            "amount_toman": 0,
            "amount_display": "۰",
            "unique_toman": 0,
            "unique_display": "+۰ تومان",
            "bank_label": "کارت مقصد ثبت نشده",
            "card_mask": "•••• ————",
            "progress": "پس از ساخت نخستین فاکتور، داده زنده اینجا نمایش داده می‌شود",
            "event_title": "هنوز تراکنشی ثبت نشده است",
            "event_subtitle": "داده‌های این بخش مستقیم از ربات دریافت می‌شوند",
            "event_icon": "•",
            "updated_at": None,
        }
    else:
        status = _status_payload(latest_invoice.status)
        card = latest_invoice.card
        amount_toman = max(0, latest_invoice.payable_amount_rial // 10)
        unique_toman = max(0, latest_invoice.unique_amount_rial // 10)
        invoice_data = {
            "exists": True,
            "status": latest_invoice.status,
            "status_label": status["label"],
            "status_tone": status["tone"],
            "amount_toman": amount_toman,
            "amount_display": fa_number(amount_toman),
            "unique_toman": unique_toman,
            "unique_display": f"+{fa_number(unique_toman)} تومان",
            "bank_label": bank_label(card.bank_code) if card else "بانک نامشخص",
            "card_mask": f"•••• {fa_digits(card.card_last4)}" if card else "•••• ————",
            "progress": status["progress"],
            "event_title": status["event_title"],
            "event_subtitle": status["event_subtitle"],
            "event_icon": status["event_icon"],
            "updated_at": (latest_invoice.paid_at or latest_invoice.updated_at or latest_invoice.created_at).isoformat(),
        }

    callback_data = _callback_payload(latest_invoice)

    return {
        "service": {
            "operational": operational,
            "label": "سامانه عملیاتی و متصل به ربات" if operational else "سامانه فعال؛ پشتیبان‌گیری نیازمند بررسی",
            "tone": "success" if operational else "warning",
            "version": APP_VERSION,
            "generated_at": now.isoformat(),
        },
        "invoice": invoice_data,
        "callback": callback_data,
        "metrics": {
            "active_merchants": active_merchants,
            "active_merchants_display": fa_number(active_merchants),
            "active_stores": active_stores,
            "active_stores_display": fa_number(active_stores),
            "active_api_keys": active_api_keys,
            "active_api_keys_display": fa_number(active_api_keys),
            "active_cards": active_cards,
            "active_cards_display": fa_number(active_cards),
            "paid_invoices": paid_invoices,
            "paid_invoices_display": fa_number(paid_invoices),
            "paid_last_24h": paid_last_24h,
            "paid_last_24h_display": fa_number(paid_last_24h),
            "pending_invoices": pending_invoices,
            "pending_invoices_display": fa_number(pending_invoices),
            "matched_sms": matched_sms,
            "matched_sms_display": fa_number(matched_sms),
            "callback_stores": callback_stores,
            "callback_stores_display": fa_number(callback_stores),
            "supported_banks": len(BANK_PROFILES),
            "supported_banks_display": fa_number(len(BANK_PROFILES)),
        },
    }
