from __future__ import annotations

from collections.abc import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.parsers import BANK_LABELS


FEE_LABELS = {
    "customer": "مشتری",
    "split": "تقسیم مساوی",
    "merchant": "پذیرنده",
}


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🧾 ایجاد فاکتور", callback_data="invoice:new")],
        [
            InlineKeyboardButton(text="💰 کیف پول", callback_data="wallet"),
            InlineKeyboardButton(text="🏦 کارت‌های بانکی", callback_data="cards"),
        ],
        [
            InlineKeyboardButton(text="🔌 اتصال و مستندات", callback_data="connect"),
            InlineKeyboardButton(text="⚙️ تنظیمات کارمزد", callback_data="fee"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="👑 مدیریت سامانه", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def connection_menu(docs_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📘 مشاهده مستندات", url=docs_url)],
            [
                InlineKeyboardButton(text="🔑 مدیریت API", callback_data="api"),
                InlineKeyboardButton(text="📲 وبهوک پیامک بانکی", callback_data="sms:webhook"),
            ],
            [InlineKeyboardButton(text="🔔 وبهوک نتیجه پرداخت", callback_data="callback:panel")],
            [InlineKeyboardButton(text="🧪 آزمایش اتصال Callback", callback_data="callback:test")],
            [InlineKeyboardButton(text="↩️ بازگشت", callback_data="home")],
        ]
    )


def callback_menu(docs_url: str, configured: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✏️ ثبت یا ویرایش آدرس", callback_data="callback:set")],
    ]
    if configured:
        rows.append([InlineKeyboardButton(text="🧪 ارسال درخواست آزمایشی", callback_data="callback:test")])
        rows.append([InlineKeyboardButton(text="🗑 حذف آدرس", callback_data="callback:remove")])
    rows.extend(
        [
            [InlineKeyboardButton(text="🔄 بازنشانی کلید امضا", callback_data="callback:secret")],
            [InlineKeyboardButton(text="📘 راهنمای بررسی امضا", url=docs_url + "#callback")],
            [InlineKeyboardButton(text="↩️ بازگشت", callback_data="connect")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sms_webhook_menu(docs_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📘 راهنمای SMS Forwarder", url=docs_url + "#sms-webhook")],
            [InlineKeyboardButton(text="↩️ بازگشت", callback_data="connect")],
        ]
    )


def fee_menu(selected: str | None = None) -> InlineKeyboardMarkup:
    def label(mode: str, text: str) -> str:
        return f"✅ {text}" if selected == mode else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label("customer", "👤 مشتری پرداخت می‌کند"), callback_data="fee:customer"),
                InlineKeyboardButton(text=label("split", "🤝 تقسیم مساوی"), callback_data="fee:split"),
            ],
            [InlineKeyboardButton(text=label("merchant", "🏪 پذیرنده پرداخت می‌کند"), callback_data="fee:merchant")],
            [InlineKeyboardButton(text="↩️ بازگشت", callback_data="home")],
        ]
    )


def cards_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ ثبت کارت جدید", callback_data="card:add")],
            [InlineKeyboardButton(text="📋 فهرست کارت‌ها", callback_data="card:list")],
            [InlineKeyboardButton(text="↩️ بازگشت", callback_data="home")],
        ]
    )


def api_menu(docs_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 ایجاد کلید API جدید", callback_data="api:regen")],
            [InlineKeyboardButton(text="📘 مستندات توسعه‌دهندگان", url=docs_url + "#api")],
            [InlineKeyboardButton(text="🔔 تنظیم Callback", callback_data="callback:panel")],
            [InlineKeyboardButton(text="↩️ بازگشت", callback_data="connect")],
        ]
    )


def flow_cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ انصراف", callback_data="flow:cancel")]]
    )


def bank_select_menu(page: int = 0) -> InlineKeyboardMarkup:
    banks = list(BANK_LABELS.items())
    page_size = 10
    total_pages = max(1, (len(banks) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    visible = banks[page * page_size : (page + 1) * page_size]

    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(visible), 2):
        rows.append([
            InlineKeyboardButton(text=f"🏦 {label}", callback_data=f"card:bank:{code}")
            for code, label in visible[index : index + 2]
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"card:bankpage:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"card:bankpage:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="✍️ بانک یا برند دیگر", callback_data="card:bank:other")])
    rows.append([InlineKeyboardButton(text="✖️ انصراف", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_fee_mode_menu(default_mode: str) -> InlineKeyboardMarkup:
    default_label = FEE_LABELS.get(default_mode, "تنظیم حساب")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 پرداخت توسط مشتری", callback_data="invoice:fee:customer"),
                InlineKeyboardButton(text="🤝 تقسیم مساوی", callback_data="invoice:fee:split"),
            ],
            [InlineKeyboardButton(text="🏪 پرداخت توسط پذیرنده", callback_data="invoice:fee:merchant")],
            [
                InlineKeyboardButton(
                    text=f"⚙️ پیش‌فرض حساب: {default_label}",
                    callback_data="invoice:fee:default",
                )
            ],
            [InlineKeyboardButton(text="✖️ انصراف از فاکتور", callback_data="flow:cancel")],
        ]
    )


def invoice_cards_menu(cards: Iterable[tuple[int, str, str, bool]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✨ انتخاب خودکار کارت", callback_data="invoice:card:0")]
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
    rows.append([InlineKeyboardButton(text="✖️ انصراف از فاکتور", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ تأیید و ایجاد فاکتور", callback_data="invoice:confirm")],
            [
                InlineKeyboardButton(text="🔄 شروع مجدد", callback_data="invoice:restart"),
                InlineKeyboardButton(text="✖️ انصراف", callback_data="flow:cancel"),
            ],
        ]
    )


def payment_created_menu(payment_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 مشاهده صفحه پرداخت", url=payment_url)],
            [InlineKeyboardButton(text="🧾 ایجاد فاکتور جدید", callback_data="invoice:new")],
            [InlineKeyboardButton(text="🏠 پنل اصلی", callback_data="home")],
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
                InlineKeyboardButton(text="🔎 بررسی پیامک‌ها", callback_data="admin:reviews"),
            ],
            [
                InlineKeyboardButton(text="🏦 کارت‌های ثبت‌شده", callback_data="admin:cards"),
                InlineKeyboardButton(text="💳 گردش کیف پول‌ها", callback_data="admin:ledger"),
            ],
            [
                InlineKeyboardButton(text="🖥 وضعیت سامانه", callback_data="admin:system"),
                InlineKeyboardButton(text="📦 انتشار نسخه جدید", callback_data="admin:update"),
            ],
            [InlineKeyboardButton(text="↩️ بازگشت", callback_data="home")],
        ]
    )
