from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, WebAppInfo
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import Merchant
from app.services.appearance_service import InlineKeyboardMarkup
from app.services.options_service import option_summary
from app.services.portal_service import merchant_portal_url

router = Router(name="commerce-options")


def options_menu(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 بازکردن مرکز آپشن‌ها", web_app=WebAppInfo(url=url))],
        [
            InlineKeyboardButton(text="📦 محصولات", callback_data="options:products"),
            InlineKeyboardButton(text="🔗 لینک‌های پرداخت", callback_data="options:links"),
        ],
        [
            InlineKeyboardButton(text="👥 مشتریان", callback_data="options:customers"),
            InlineKeyboardButton(text="⚡ اتوماسیون‌ها", callback_data="options:automations"),
        ],
        [
            InlineKeyboardButton(text="🔌 اتصال‌ها", callback_data="options:connectors"),
            InlineKeyboardButton(text="📣 کمپین‌ها", callback_data="options:campaigns"),
        ],
        [
            InlineKeyboardButton(text="🎟 تخفیف و معرف", callback_data="options:discounts"),
            InlineKeyboardButton(text="🔄 اشتراک‌ها", callback_data="options:subscriptions"),
        ],
        [
            InlineKeyboardButton(text="🧾 قالب و زمان‌بندی", callback_data="options:templates"),
            InlineKeyboardButton(text="📨 درخواست پرداخت", callback_data="options:requests"),
        ],
        [
            InlineKeyboardButton(text="🏬 شعبه و صندوق", callback_data="options:branches"),
            InlineKeyboardButton(text="🛡 ضدتقلب", callback_data="options:fraud"),
        ],
        [InlineKeyboardButton(text="⌂ بازگشت به صفحه اصلی", callback_data="home")],
    ])


@router.callback_query(F.data == "options")
async def options_panel(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("ابتدا حساب پذیرنده را فعال کنید.", show_alert=True)
        summary = await option_summary(session, merchant.id)
        await session.commit()
        url = merchant_portal_url(merchant) + "/options"
    text = (
        "🧩 <b>مرکز آپشن‌های بلوپی</b>\n\n"
        f"📦 محصولات: <b>{summary['products']:,}</b>\n"
        f"👥 مشتریان: <b>{summary['customers']:,}</b>\n"
        f"🔗 لینک‌های پرداخت: <b>{summary['payment_links']:,}</b>\n"
        f"⚡ اتوماسیون‌ها: <b>{summary['automation_rules']:,}</b>\n"
        f"🔌 اتصال‌های فعال: <b>{summary['connectors']:,}</b>\n"
        f"📥 موارد نیازمند اقدام: <b>{summary['inbox_open']:,}</b>\n\n"
        "از پنل وب می‌توانید محصولات، لینک دائمی، پرداخت چندتکه، مشتریان، "
        "بازپرداخت، تخفیف، همکاری در فروش، اشتراک، درخواست پرداخت، شعبه، کمپین، اتصال‌ها، قوانین ضدتقلب و اتوماسیون بعد از پرداخت را مدیریت کنید."
    )
    await callback.message.edit_text(text, reply_markup=options_menu(url))
    await callback.answer()


@router.callback_query(F.data.startswith("options:"))
async def options_shortcut(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        url = merchant_portal_url(merchant) + "/options"
    labels = {
        "products": "محصولات و تحویل خودکار",
        "links": "لینک‌های پرداخت و QR",
        "customers": "دفترچه و پورتال مشتریان",
        "automations": "اتوماسیون‌های بدون کدنویسی",
        "connectors": "اتصال‌های WHMCS، Marzban، Pasarguard و Webhook",
        "campaigns": "کمپین‌ها و تحلیل نرخ تبدیل",
        "discounts": "کدهای تخفیف و همکاری در فروش",
        "subscriptions": "اشتراک‌ها و تمدید خودکار",
        "templates": "قالب و زمان‌بندی فاکتور",
        "requests": "درخواست‌های پرداخت",
        "branches": "شعبه‌ها و صندوق فروشگاهی",
        "fraud": "قوانین ضدتقلب و بررسی دستی",
    }
    key = callback.data.split(":", 1)[1]
    await callback.message.edit_text(
        f"🧩 <b>{labels.get(key, 'مرکز آپشن‌ها')}</b>\n\nبرای مدیریت کامل این بخش، مرکز آپشن‌های وب را باز کنید.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 بازکردن این بخش", web_app=WebAppInfo(url=url + f"#{key}"))],
            [InlineKeyboardButton(text="‹ بازگشت", callback_data="options")],
        ]),
    )
    await callback.answer()
