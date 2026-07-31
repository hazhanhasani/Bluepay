from __future__ import annotations

import html

import httpx

from app.bot.presentation import badge, money_toman, panel, sms_result_label
from app.core.config import settings
from app.db.session import SessionLocal
from app.models import Invoice, Merchant, SmsTransaction
from sqlalchemy import select
from app.services.sms_service import SmsIngestDiagnostic
from app.version import APP_VERSION


def _money(value: int | None) -> str:
    return "نامشخص" if value is None else money_toman(value)


def _diagnostic_line(sms: SmsTransaction, diagnostic: SmsIngestDiagnostic) -> str:
    return (
        f"کد بررسی: <code>{html.escape(diagnostic.result)}</code>  •  "
        f"اطمینان: <b>{sms.parse_confidence}%</b>  •  "
        f"نسخه: <code>{APP_VERSION}</code>"
    )


async def _send_telegram(chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{settings.bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )


async def _send_wallet_topup_notice(invoice: Invoice) -> None:
    if not invoice.wallet_target_merchant_id:
        return
    async with SessionLocal() as session:
        target = await session.scalar(select(Merchant).where(Merchant.id == invoice.wallet_target_merchant_id))
    if not target:
        return
    text = panel(
        "💰",
        "کیف پول با موفقیت شارژ شد",
        [
            f"➕ اعتبار افزوده‌شده: <b>{money_toman(invoice.payable_amount_rial)}</b>",
            f"💼 موجودی جدید: <b>{money_toman(target.wallet_balance_rial)}</b>",
            f"🧾 شناسه شارژ: <code>{html.escape(invoice.order_id)}</code>",
            "📡 وضعیت: <b>تأیید بانکی</b>",
        ],
        subtitle="واریز بانکی شناسایی و اعتبار کیف پول ثبت شد",
        footer="تمام مبلغ دقیق پرداخت‌شده، شامل کد تطبیق، به کیف پول اضافه شد.",
    )
    try:
        await _send_telegram(target.telegram_user_id, text)
    except Exception as exc:
        print(f"wallet_topup_notice_error={type(exc).__name__}: {exc}")


async def send_sms_processing_notice(
    merchant: Merchant,
    sms: SmsTransaction,
    invoice: Invoice | None,
    diagnostic: SmsIngestDiagnostic,
) -> None:
    if diagnostic.result == "duplicate":
        return

    if invoice and invoice.purpose == "wallet_topup":
        text = panel(
            "💰",
            "شارژ کیف پول تأیید شد",
            [
                f"🧾 شناسه: <code>{html.escape(invoice.order_id)}</code>",
                f"💳 مبلغ: <b>{_money(sms.amount_rial)}</b>",
                f"🏦 بانک: <b>{html.escape(sms.bank_code)}</b>",
                f"📱 منبع پیامک: <code>{html.escape(sms.device_id or 'ثبت نشده')}</code>",
                f"📡 وضعیت: <b>{badge('paid')}</b>",
            ],
            subtitle="واریز شارژ با حساب دریافت سامانه تطبیق داده شد",
            footer=_diagnostic_line(sms, diagnostic),
        )
    elif invoice:
        text = panel(
            "✅",
            "پرداخت تأیید شد",
            [
                f"🧾 سفارش: <code>{html.escape(invoice.order_id)}</code>",
                f"💳 مبلغ: <b>{_money(sms.amount_rial)}</b>",
                f"🏦 بانک: <b>{html.escape(sms.bank_code)}</b>",
                f"📱 منبع پیامک: <code>{html.escape(sms.device_id or 'ثبت نشده')}</code>",
                f"📡 وضعیت: <b>{badge('paid')}</b>",
            ],
            subtitle="تراکنش بانکی با فاکتور در انتظار تطبیق داده شد",
            footer=_diagnostic_line(sms, diagnostic),
        )
    else:
        lines = [
            f"🏦 بانک تشخیص‌داده‌شده: <b>{html.escape(sms.bank_code)}</b>",
            f"💵 مبلغ: <b>{_money(sms.amount_rial)}</b>",
            f"📱 منبع پیامک: <code>{html.escape(sms.device_id or 'ثبت نشده')}</code>",
            f"🔎 نتیجه: <b>{html.escape(sms_result_label(diagnostic.result))}</b>",
            f"📝 توضیح: {html.escape(diagnostic.detail)}",
        ]
        preview = html.escape((sms.raw_message or "").strip()[:420])
        if preview:
            lines.extend(["", "<b>متن دریافت‌شده</b>", f"<blockquote>{preview}</blockquote>"])
        text = panel(
            "⚠️",
            "پیامک دریافت شد؛ پرداخت تأیید نشد",
            lines,
            subtitle="برای جلوگیری از تأیید اشتباه، فاکتور بدون تطبیق دقیق پرداخت نمی‌شود",
            footer=_diagnostic_line(sms, diagnostic),
        )

    try:
        if invoice and invoice.purpose == "wallet_topup" and invoice.wallet_target_merchant_id == merchant.id:
            await _send_wallet_topup_notice(invoice)
        else:
            await _send_telegram(merchant.telegram_user_id, text)
            if invoice and invoice.purpose == "wallet_topup":
                await _send_wallet_topup_notice(invoice)
    except Exception as exc:
        print(f"sms_notice_error={type(exc).__name__}: {exc}")


async def send_invalid_sms_payload_notice(
    merchant: Merchant,
    error_code: str,
    detail: str,
    preview: str = "",
) -> None:
    safe_preview = html.escape((preview or "").strip()[:360])
    lines = [
        f"🔧 کد خطا: <code>{html.escape(error_code)}</code>",
        f"📝 علت: {html.escape(detail)}",
        "",
        "<b>تنظیم استاندارد SMS Forwarder</b>",
        "• Method: <code>POST</code>",
        "• Body: <code>JSON</code>",
        '• Payload: <code>{"device_id":"phone-1","sender":"{in-number}","message":"{msg}"}</code>',
        "",
        "متغیرهای داخل آکولاد باید با دکمه <b>{}</b> خود برنامه درج شوند.",
    ]
    if safe_preview:
        lines.extend(["", "<b>بدنه دریافت‌شده</b>", f"<blockquote>{safe_preview}</blockquote>"])
    text = panel(
        "❌",
        "بدنه وبهوک پیامک معتبر نیست",
        lines,
        subtitle="درخواست دریافت شد اما اطلاعات واقعی پیامک در آن وجود نداشت",
        footer="پس از اصلاح تنظیمات، یک پیامک جدید ارسال کنید؛ Retry درخواست قدیمی ممکن است همان بدنه قبلی را تکرار کند.",
    )

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
