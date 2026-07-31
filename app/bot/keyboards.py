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
        rows.append([InlineKeyboardButton(text="📦 آپدیت سیستم", callback_data="update:help")])
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
