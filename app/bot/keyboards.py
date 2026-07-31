from __future__ import annotations

from collections.abc import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


FEE_LABELS = {
    "customer": "👤 مشتری",
    "split": "🤝 نصف‌نصف",
    "merchant": "🏪 پذیرنده",
}

BANK_LABELS = {
    "mellat": "ملت",
    "melli": "ملی",
    "blu": "بلو",
    "pasargad": "پاسارگاد",
    "saman": "سامان",
    "tejarat": "تجارت",
    "parsian": "پارسیان",
    "sepah": "سپاه",
    "refah": "رفاه",
    "keshavarzi": "کشاورزی",
}


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🧾  ساخت فاکتور جدید", callback_data="invoice:new")],
        [
            InlineKeyboardButton(text="💰  کیف پول", callback_data="wallet"),
            InlineKeyboardButton(text="🏦  کارت‌های من", callback_data="cards"),
        ],
        [
            InlineKeyboardButton(text="🔑  API پذیرنده", callback_data="api"),
            InlineKeyboardButton(text="⚙️  تنظیم کارمزد", callback_data="fee"),
        ],
        [InlineKeyboardButton(text="🔗  اتصال پیامک بانکی", callback_data="sms:webhook")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="👑  مرکز مدیریت", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fee_menu(selected: str | None = None) -> InlineKeyboardMarkup:
    def label(mode: str, text: str) -> str:
        return f"✅ {text}" if selected == mode else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label("customer", "👤 کامل با مشتری"), callback_data="fee:customer"),
                InlineKeyboardButton(text=label("split", "🤝 نصف‌نصف"), callback_data="fee:split"),
            ],
            [InlineKeyboardButton(text=label("merchant", "🏪 کامل با پذیرنده"), callback_data="fee:merchant")],
            [InlineKeyboardButton(text="↩️ بازگشت به پنل", callback_data="home")],
        ]
    )


def cards_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ افزودن کارت جدید", callback_data="card:add")],
            [InlineKeyboardButton(text="📋 مشاهده کارت‌های ثبت‌شده", callback_data="card:list")],
            [InlineKeyboardButton(text="↩️ بازگشت به پنل", callback_data="home")],
        ]
    )


def api_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 ساخت یا تعویض API Key", callback_data="api:regen")],
            [InlineKeyboardButton(text="↩️ بازگشت به پنل", callback_data="home")],
        ]
    )


def flow_cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ لغو عملیات", callback_data="flow:cancel")]]
    )


def bank_select_menu() -> InlineKeyboardMarkup:
    banks = list(BANK_LABELS.items())
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(banks), 2):
        row = [
            InlineKeyboardButton(
                text=f"🏦 {BANK_LABELS[code]}",
                callback_data=f"card:bank:{code}",
            )
            for code, _ in banks[index : index + 2]
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✍️ بانک دیگر", callback_data="card:bank:other")])
    rows.append([InlineKeyboardButton(text="❌ لغو", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_fee_mode_menu(default_mode: str) -> InlineKeyboardMarkup:
    default_label = FEE_LABELS.get(default_mode, "تنظیم حساب")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 مشتری پرداخت کند", callback_data="invoice:fee:customer"),
                InlineKeyboardButton(text="🤝 نصف‌نصف", callback_data="invoice:fee:split"),
            ],
            [InlineKeyboardButton(text="🏪 پذیرنده پرداخت کند", callback_data="invoice:fee:merchant")],
            [
                InlineKeyboardButton(
                    text=f"⚙️ پیش‌فرض حساب: {default_label}",
                    callback_data="invoice:fee:default",
                )
            ],
            [InlineKeyboardButton(text="❌ لغو فاکتور", callback_data="flow:cancel")],
        ]
    )


def invoice_cards_menu(cards: Iterable[tuple[int, str, str, bool]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✨ انتخاب هوشمند کارت", callback_data="invoice:card:0")]
    ]
    for card_id, bank_label, last4, is_default in cards:
        star = "⭐ " if is_default else "💳 "
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{star}{bank_label}  •••• {last4}",
                    callback_data=f"invoice:card:{card_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="❌ لغو فاکتور", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ تأیید و ساخت فاکتور", callback_data="invoice:confirm")],
            [
                InlineKeyboardButton(text="🔄 شروع دوباره", callback_data="invoice:restart"),
                InlineKeyboardButton(text="❌ لغو", callback_data="flow:cancel"),
            ],
        ]
    )


def payment_created_menu(payment_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 باز کردن صفحه پرداخت", url=payment_url)],
            [InlineKeyboardButton(text="🧾 ساخت فاکتور دیگر", callback_data="invoice:new")],
            [InlineKeyboardButton(text="🏠 بازگشت به پنل", callback_data="home")],
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
