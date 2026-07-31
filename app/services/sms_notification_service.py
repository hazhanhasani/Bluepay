from __future__ import annotations

import html

import httpx

from app.bot.presentation import sms_result_label
from app.core.config import settings
from app.models import Invoice, Merchant, SmsTransaction
from app.services.sms_service import SmsIngestDiagnostic
from app.version import APP_VERSION


def _toman(value: int | None) -> str:
    if value is None:
        return "نامشخص"
    return f"{value // 10:,} تومان"


def _technical_detail(sms: SmsTransaction, diagnostic: SmsIngestDiagnostic) -> str:
    return (
        f"🔧 کد بررسی: <code>{html.escape(diagnostic.result)}</code>\n"
        f"🧠 اطمینان تشخیص: <b>{sms.parse_confidence}%</b>\n"
        f"🧩 نسخه پردازشگر: <code>{APP_VERSION}</code>"
    )


async def send_sms_processing_notice(
    merchant: Merchant,
    sms: SmsTransaction,
    invoice: Invoice | None,
    diagnostic: SmsIngestDiagnostic,
) -> None:
    if diagnostic.result == "duplicate":
        return

    if invoice:
        text = (
            "✅ <b>پرداخت با موفقیت تأیید شد</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"🧾 شناسه سفارش: <code>{html.escape(invoice.order_id)}</code>\n"
            f"💳 مبلغ پرداخت: <b>{_toman(sms.amount_rial)}</b>\n"
            f"🏦 بانک: <b>{html.escape(sms.bank_code)}</b>\n"
            f"📱 منبع پیامک: <code>{html.escape(sms.device_id or 'ثبت نشده')}</code>\n"
            "━━━━━━━━━━━━━━━━\n"
            "پرداخت به‌صورت خودکار با فاکتور در انتظار تطبیق داده شد.\n\n"
            + _technical_detail(sms, diagnostic)
        )
    else:
        text = (
            "⚠️ <b>پیامک دریافت شد؛ پرداخت تأیید نشد</b>\n"
            "━━━━━━━━━━━━━━━━\n"
            f"🏦 بانک: <b>{html.escape(sms.bank_code)}</b>\n"
            f"💵 مبلغ تشخیص‌داده‌شده: <b>{_toman(sms.amount_rial)}</b>\n"
            f"📱 منبع پیامک: <code>{html.escape(sms.device_id or 'ثبت نشده')}</code>\n"
            f"🔎 نتیجه بررسی: <b>{html.escape(sms_result_label(diagnostic.result))}</b>\n"
            f"📝 توضیح: {html.escape(diagnostic.detail)}\n\n"
            + _technical_detail(sms, diagnostic)
        )
        preview = html.escape((sms.raw_message or "").strip()[:350])
        if preview:
            text += f"\n\n<b>متن دریافت‌شده</b>\n<blockquote>{preview}</blockquote>"

    url = f"https://api.telegram.org/bot{settings.bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                url,
                json={
                    "chat_id": merchant.telegram_user_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as exc:
        print(f"sms_notice_error={type(exc).__name__}: {exc}")


async def send_invalid_sms_payload_notice(
    merchant: Merchant,
    error_code: str,
    detail: str,
    preview: str = "",
) -> None:
    safe_preview = html.escape((preview or "").strip()[:300])
    text = (
        "❌ <b>درخواست پیامک قابل پردازش نیست</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🔧 کد خطا: <code>{html.escape(error_code)}</code>\n"
        f"📝 علت: {html.escape(detail)}\n"
        "━━━━━━━━━━━━━━━━\n"
        "در SMS Forwarder، نوع Body را روی <code>JSON</code> قرار دهید و متغیرها را با دکمه <b>{}</b> برنامه درج کنید.\n\n"
        '<code>{"device_id":"phone-1","sender":"{in-number}","message":"{msg}"}</code>'
    )
    if safe_preview:
        text += f"\n\n<b>بدنه دریافت‌شده</b>\n<blockquote>{safe_preview}</blockquote>"

    url = f"https://api.telegram.org/bot{settings.bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                url,
                json={
                    "chat_id": merchant.telegram_user_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as exc:
        print(f"invalid_sms_payload_notice_error={type(exc).__name__}: {exc}")
