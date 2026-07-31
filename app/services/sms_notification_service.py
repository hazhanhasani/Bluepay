from __future__ import annotations

import html

import httpx

from app.core.config import settings
from app.models import Invoice, Merchant, SmsTransaction
from app.services.sms_service import SmsIngestDiagnostic


def _toman(value: int | None) -> str:
    if value is None:
        return "نامشخص"
    return f"{value // 10:,} تومان"


async def send_sms_processing_notice(
    merchant: Merchant,
    sms: SmsTransaction,
    invoice: Invoice | None,
    diagnostic: SmsIngestDiagnostic,
) -> None:
    icon = "✅" if diagnostic.result == "matched" else "⚠️"
    title = "پرداخت تأیید شد" if invoice else "پیامک رسید اما فاکتور تأیید نشد"
    text = (
        f"{icon} <b>{title}</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🏦 بانک تشخیص‌داده‌شده: <code>{html.escape(sms.bank_code)}</code>\n"
        f"💵 مبلغ: <b>{_toman(sms.amount_rial)}</b>\n"
        f"📱 منبع: <code>{html.escape(sms.device_id or 'ثبت نشده')}</code>\n"
        f"🧠 اطمینان: <b>{sms.parse_confidence}%</b>\n"
        f"📌 نتیجه: <code>{html.escape(diagnostic.result)}</code>\n"
        f"📝 توضیح: {html.escape(diagnostic.detail)}"
    )
    if invoice:
        text += f"\n🧾 شناسه سفارش: <code>{html.escape(invoice.order_id)}</code>"

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
