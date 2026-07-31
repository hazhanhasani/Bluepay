from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🧾 ساخت فاکتور دستی", callback_data="invoice:new")],
        [
            InlineKeyboardButton(text="💰 کیف پول", callback_data="wallet"),
            InlineKeyboardButton(text="🏦 کارت‌ها", callback_data="cards"),
        ],
        [
            InlineKeyboardButton(text="🔑 API", callback_data="api"),
            InlineKeyboardButton(text="⚙️ کارمزد", callback_data="fee"),
        ],
        [InlineKeyboardButton(text="🔗 اطلاعات وب‌هوک پیامک", callback_data="sms:webhook")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="👑 بخش مدیریت", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fee_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 کامل با مشتری", callback_data="fee:customer")],
            [InlineKeyboardButton(text="🤝 نصف‌نصف", callback_data="fee:split")],
            [InlineKeyboardButton(text="🏪 کامل با پذیرنده", callback_data="fee:merchant")],
            [InlineKeyboardButton(text="↩️ بازگشت", callback_data="home")],
        ]
    )


def cards_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ افزودن کارت", callback_data="card:add")],
            [InlineKeyboardButton(text="📋 فهرست کارت‌ها", callback_data="card:list")],
            [InlineKeyboardButton(text="↩️ بازگشت", callback_data="home")],
        ]
    )


def api_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 ساخت/تعویض API Key", callback_data="api:regen")],
            [InlineKeyboardButton(text="↩️ بازگشت", callback_data="home")],
        ]
    )


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 داشبورد", callback_data="admin:dashboard"),
                InlineKeyboardButton(text="👥 پذیرندگان", callback_data="admin:merchants:0"),
            ],
            [
                InlineKeyboardButton(text="🧾 فاکتورها", callback_data="admin:invoices"),
                InlineKeyboardButton(text="🔎 بررسی پیامک", callback_data="admin:reviews"),
            ],
            [
                InlineKeyboardButton(text="🏦 همه کارت‌ها", callback_data="admin:cards"),
                InlineKeyboardButton(text="💳 گردش کیف پول", callback_data="admin:ledger"),
            ],
            [
                InlineKeyboardButton(text="🖥 وضعیت سیستم", callback_data="admin:system"),
                InlineKeyboardButton(text="📦 آپدیت سیستم", callback_data="admin:update"),
            ],
            [InlineKeyboardButton(text="↩️ بازگشت به پنل", callback_data="home")],
        ]
    )
