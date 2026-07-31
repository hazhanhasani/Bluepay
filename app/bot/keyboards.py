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
        [InlineKeyboardButton(text="➕ ساخت فاکتور جدید", callback_data="invoice:new")],
        [
            InlineKeyboardButton(text="💼 کیف پول", callback_data="wallet"),
            InlineKeyboardButton(text="💳 کارت‌های مقصد", callback_data="cards"),
        ],
        [
            InlineKeyboardButton(text="⚙️ سیاست کارمزد", callback_data="fee"),
            InlineKeyboardButton(text="🔗 اتصال و API", callback_data="connect"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="👑 مرکز مدیریت سامانه", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def connection_menu(docs_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 بازکردن مستندات اختصاصی", url=docs_url)],
            [
                InlineKeyboardButton(text="🔑 کلید و API", callback_data="api"),
                InlineKeyboardButton(text="📲 وبهوک پیامک", callback_data="sms:webhook"),
            ],
            [InlineKeyboardButton(text="🔔 Callback پرداخت", callback_data="callback:panel")],
            [InlineKeyboardButton(text="🧪 آزمایش Callback", callback_data="callback:test")],
            [InlineKeyboardButton(text="‹ بازگشت به داشبورد", callback_data="home")],
        ]
    )


def callback_menu(docs_url: str, configured: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✏️ ثبت یا ویرایش نشانی", callback_data="callback:set")]]
    if configured:
        rows.extend(
            [
                [InlineKeyboardButton(text="🧪 ارسال رویداد آزمایشی", callback_data="callback:test")],
                [InlineKeyboardButton(text="🗑 حذف نشانی Callback", callback_data="callback:remove")],
            ]
        )
    rows.extend(
        [
            [InlineKeyboardButton(text="🔐 بازنشانی Secret امضا", callback_data="callback:secret")],
            [InlineKeyboardButton(text="📖 راهنمای اعتبارسنجی امضا", url=docs_url + "#callback")],
            [InlineKeyboardButton(text="‹ بازگشت به مرکز اتصال", callback_data="connect")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sms_webhook_menu(docs_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📖 آموزش تصویری SMS Forwarder", url=docs_url + "#sms-webhook")],
            [InlineKeyboardButton(text="‹ بازگشت به مرکز اتصال", callback_data="connect")],
        ]
    )


def fee_menu(selected: str | None = None) -> InlineKeyboardMarkup:
    def label(mode: str, text: str) -> str:
        return f"✓ {text}" if selected == mode else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label("customer", "👤 پرداخت کامل توسط مشتری"), callback_data="fee:customer")],
            [InlineKeyboardButton(text=label("split", "🤝 تقسیم مساوی هزینه"), callback_data="fee:split")],
            [InlineKeyboardButton(text=label("merchant", "🏪 پرداخت کامل توسط پذیرنده"), callback_data="fee:merchant")],
            [InlineKeyboardButton(text="‹ بازگشت به داشبورد", callback_data="home")],
        ]
    )


def cards_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="＋ افزودن کارت مقصد", callback_data="card:add")],
            [InlineKeyboardButton(text="💳 مشاهده کارت‌های ثبت‌شده", callback_data="card:list")],
            [InlineKeyboardButton(text="‹ بازگشت به داشبورد", callback_data="home")],
        ]
    )


def api_menu(docs_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 ایجاد یا تعویض کلید API", callback_data="api:regen")],
            [InlineKeyboardButton(text="📚 مستندات API", url=docs_url + "#api")],
            [InlineKeyboardButton(text="🔔 تنظیم Callback", callback_data="callback:panel")],
            [InlineKeyboardButton(text="‹ بازگشت به مرکز اتصال", callback_data="connect")],
        ]
    )


def flow_cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="× لغو و بازگشت به داشبورد", callback_data="flow:cancel")]]
    )


def bank_select_menu(page: int = 0) -> InlineKeyboardMarkup:
    banks = list(BANK_LABELS.items())
    page_size = 10
    total_pages = max(1, (len(banks) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    visible = banks[page * page_size : (page + 1) * page_size]

    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(visible), 2):
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"card:bank:{code}")
                for code, label in visible[index : index + 2]
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹ قبلی", callback_data=f"card:bankpage:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"صفحه {page+1} از {total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="بعدی ›", callback_data=f"card:bankpage:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="✍️ بانک یا برند دیگر", callback_data="card:bank:other")])
    rows.append([InlineKeyboardButton(text="× لغو ثبت کارت", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_fee_mode_menu(default_mode: str) -> InlineKeyboardMarkup:
    default_label = FEE_LABELS.get(default_mode, "تنظیم حساب")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 مشتری پرداخت می‌کند", callback_data="invoice:fee:customer")],
            [InlineKeyboardButton(text="🤝 هزینه به‌صورت مساوی تقسیم شود", callback_data="invoice:fee:split")],
            [InlineKeyboardButton(text="🏪 پذیرنده پرداخت می‌کند", callback_data="invoice:fee:merchant")],
            [
                InlineKeyboardButton(
                    text=f"⚙️ استفاده از پیش‌فرض حساب: {default_label}",
                    callback_data="invoice:fee:default",
                )
            ],
            [InlineKeyboardButton(text="× لغو ساخت فاکتور", callback_data="flow:cancel")],
        ]
    )


def invoice_cards_menu(cards: Iterable[tuple[int, str, str, bool]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✨ انتخاب هوشمند کارت مقصد", callback_data="invoice:card:0")]
    ]
    for card_id, bank_label, last4, is_default in cards:
        marker = "⭐" if is_default else "💳"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} {bank_label}  •••• {last4}",
                    callback_data=f"invoice:card:{card_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="× لغو ساخت فاکتور", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✓ تأیید و صدور لینک پرداخت", callback_data="invoice:confirm")],
            [
                InlineKeyboardButton(text="↻ شروع دوباره", callback_data="invoice:restart"),
                InlineKeyboardButton(text="× لغو", callback_data="flow:cancel"),
            ],
        ]
    )


def payment_created_menu(payment_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 بازکردن صفحه پرداخت", url=payment_url)],
            [InlineKeyboardButton(text="＋ ساخت فاکتور دیگری", callback_data="invoice:new")],
            [InlineKeyboardButton(text="⌂ بازگشت به داشبورد", callback_data="home")],
        ]
    )


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 نمای مدیریتی", callback_data="admin:dashboard")],
            [
                InlineKeyboardButton(text="👥 پذیرندگان", callback_data="admin:merchants:0"),
                InlineKeyboardButton(text="🧾 فاکتورها", callback_data="admin:invoices"),
            ],
            [
                InlineKeyboardButton(text="🔎 بررسی تراکنش‌ها", callback_data="admin:reviews"),
                InlineKeyboardButton(text="💳 کارت‌ها", callback_data="admin:cards"),
            ],
            [
                InlineKeyboardButton(text="💼 گردش کیف پول", callback_data="admin:ledger"),
                InlineKeyboardButton(text="🖥 سلامت سامانه", callback_data="admin:system"),
            ],
            [InlineKeyboardButton(text="📦 مدیریت انتشار نسخه", callback_data="admin:update")],
            [InlineKeyboardButton(text="‹ بازگشت به داشبورد پذیرنده", callback_data="home")],
        ]
    )
