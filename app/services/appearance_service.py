from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup as TelegramInlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_service import get_setting, set_setting

THEME_SEMANTIC = "semantic"
THEME_DEFAULT = "default"
THEME_PRIMARY = "primary"
THEME_SUCCESS = "success"
THEME_DANGER = "danger"
VALID_BUTTON_THEMES = {
    THEME_SEMANTIC,
    THEME_DEFAULT,
    THEME_PRIMARY,
    THEME_SUCCESS,
    THEME_DANGER,
}

THEME_LABELS = {
    THEME_SEMANTIC: "هوشمند و کاربردی",
    THEME_DEFAULT: "پیش‌فرض تلگرام",
    THEME_PRIMARY: "همه دکمه‌ها آبی",
    THEME_SUCCESS: "همه دکمه‌ها سبز",
    THEME_DANGER: "همه دکمه‌ها قرمز",
}

# slot: (عنوان مدیریت، ایموجی عادی جایگزین)
EMOJI_SLOTS: dict[str, tuple[str, str]] = {
    "create": ("ساخت و افزودن", "➕"),
    "invoice": ("فاکتور و پرداخت", "🧾"),
    "wallet": ("کیف پول", "💼"),
    "card": ("کارت بانکی", "💳"),
    "store": ("فروشگاه", "🏪"),
    "api": ("API و اتصال", "🔗"),
    "account": ("حساب پذیرنده", "👤"),
    "admin": ("مدیریت سامانه", "👑"),
    "settings": ("تنظیمات", "⚙️"),
    "docs": ("راهنما و مستندات", "📚"),
    "sms": ("پیامک بانکی", "📲"),
    "callback": ("Callback", "🔔"),
    "home": ("صفحه اصلی", "⌂"),
    "back": ("بازگشت", "↩️"),
    "success": ("تأیید و فعال‌سازی", "✅"),
    "danger": ("حذف و غیرفعال‌سازی", "⛔"),
}

SETTING_THEME = "ui_button_theme"
SETTING_PREMIUM_ENABLED = "ui_premium_emoji_enabled"
SETTING_EMOJI_PREFIX = "ui_custom_emoji_"


@dataclass(slots=True)
class AppearanceConfig:
    button_theme: str = THEME_SEMANTIC
    premium_emoji_enabled: bool = False
    emoji_ids: dict[str, str] = field(default_factory=dict)


_cache = AppearanceConfig()


def get_appearance() -> AppearanceConfig:
    return _cache


def _as_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _normal_theme(value: str | None) -> str:
    return value if value in VALID_BUTTON_THEMES else THEME_SEMANTIC


async def load_appearance_settings(session: AsyncSession) -> AppearanceConfig:
    global _cache
    theme = _normal_theme(await get_setting(session, SETTING_THEME, THEME_SEMANTIC))
    enabled = _as_bool(await get_setting(session, SETTING_PREMIUM_ENABLED, "0"))
    emoji_ids: dict[str, str] = {}
    for slot in EMOJI_SLOTS:
        value = (await get_setting(session, f"{SETTING_EMOJI_PREFIX}{slot}", "") or "").strip()
        if value:
            emoji_ids[slot] = value
    _cache = AppearanceConfig(button_theme=theme, premium_emoji_enabled=enabled, emoji_ids=emoji_ids)
    return _cache


async def set_button_theme(session: AsyncSession, theme: str) -> AppearanceConfig:
    global _cache
    theme = _normal_theme(theme)
    await set_setting(session, SETTING_THEME, theme)
    _cache = AppearanceConfig(theme, _cache.premium_emoji_enabled, dict(_cache.emoji_ids))
    return _cache


async def set_premium_emoji_enabled(session: AsyncSession, enabled: bool) -> AppearanceConfig:
    global _cache
    await set_setting(session, SETTING_PREMIUM_ENABLED, "1" if enabled else "0")
    _cache = AppearanceConfig(_cache.button_theme, bool(enabled), dict(_cache.emoji_ids))
    return _cache


async def set_custom_emoji(session: AsyncSession, slot: str, emoji_id: str | None) -> AppearanceConfig:
    global _cache
    if slot not in EMOJI_SLOTS:
        raise ValueError("invalid emoji slot")
    clean = (emoji_id or "").strip()
    await set_setting(session, f"{SETTING_EMOJI_PREFIX}{slot}", clean)
    ids = dict(_cache.emoji_ids)
    if clean:
        ids[slot] = clean
    else:
        ids.pop(slot, None)
    _cache = AppearanceConfig(_cache.button_theme, _cache.premium_emoji_enabled, ids)
    return _cache


async def reset_appearance(session: AsyncSession) -> AppearanceConfig:
    global _cache
    await set_setting(session, SETTING_THEME, THEME_SEMANTIC)
    await set_setting(session, SETTING_PREMIUM_ENABLED, "0")
    for slot in EMOJI_SLOTS:
        await set_setting(session, f"{SETTING_EMOJI_PREFIX}{slot}", "")
    _cache = AppearanceConfig()
    return _cache


def _contains_any(value: str, words: tuple[str, ...]) -> bool:
    lowered = value.casefold()
    return any(word.casefold() in lowered for word in words)


def infer_button_role(button: InlineKeyboardButton) -> str | None:
    text = button.text or ""
    data = button.callback_data or ""
    joined = f"{text} {data}"

    if data == "noop":
        return None
    if _contains_any(
        joined,
        (
            "delete",
            "remove",
            "debit",
            "cancel",
            "reject",
            "غیرفعال",
            "حذف",
            "لغو",
            "کسر",
            "رد پرداخت",
            "ابطال",
            ":danger",
        ),
    ):
        return THEME_DANGER
    if _contains_any(
        joined,
        (
            ":new",
            ":add",
            ":confirm",
            ":approve",
            ":topup",
            ":apply:confirm",
            ":success",
            "فعال‌کردن",
            "تأیید",
            "صدور",
            "ساخت",
            "افزودن",
            "شارژ",
            "ثبت فروشگاه",
        ),
    ):
        return THEME_SUCCESS
    if button.url or _contains_any(
        joined,
        (
            "admin:",
            "invoice",
            "wallet",
            "cards",
            "connect",
            "stores",
            "store:",
            "api",
            "account",
            "dashboard",
            "invoices",
            "reviews",
            "system",
            "ledger",
            "callback",
            "sms:webhook",
        ),
    ):
        return THEME_PRIMARY
    return None


def infer_emoji_slot(button: InlineKeyboardButton) -> str | None:
    text = button.text or ""
    data = button.callback_data or ""
    joined = f"{text} {data}"

    if _contains_any(joined, ("delete", "remove", "debit", "cancel", "reject", ":danger", "غیرفعال", "حذف", "لغو", "کسر")):
        return "danger"
    if _contains_any(joined, (":confirm", ":approve", ":success", "فعال‌کردن", "تأیید", "اعمال به همه")):
        return "success"
    if data == "home" or "صفحه اصلی" in text:
        return "home"
    if _contains_any(text, ("بازگشت", "فهرست")) or data.startswith(("back", "admin:panel")):
        return "back" if "بازگشت" in text or "فهرست" in text else "admin"
    if _contains_any(joined, (":new", ":add", ":topup", "ساخت", "افزودن", "شارژ آنلاین")):
        return "create"
    if _contains_any(joined, ("invoice", "invoices", "فاکتور", "پرداخت")):
        return "invoice"
    if _contains_any(joined, ("wallet", "ledger", "کیف پول", "اعتبار")):
        return "wallet"
    if _contains_any(joined, ("card", "کارت")):
        return "card"
    if _contains_any(joined, ("store", "فروشگاه")):
        return "store"
    if _contains_any(joined, ("sms", "پیامک")):
        return "sms"
    if _contains_any(joined, ("callback", "Callback")):
        return "callback"
    if _contains_any(joined, ("api", "connect", "اتصال", "کلید")):
        return "api"
    if _contains_any(joined, ("account", "پذیرنده")):
        return "account"
    if _contains_any(joined, ("docs", "راهنما", "مستندات", "آموزش")) or button.url:
        return "docs"
    if _contains_any(joined, ("settings", "تنظیم", "پیش‌فرض", "کارمزد")):
        return "settings"
    if _contains_any(joined, ("admin", "مدیریت", "مدیر")):
        return "admin"
    return None


_DECORATIVE_PREFIXES = tuple(
    sorted(
        {
            "➕",
            "＋",
            "🧾",
            "💼",
            "💳",
            "⚙️",
            "🔗",
            "👤",
            "👑",
            "⌂",
            "📚",
            "🏪",
            "📲",
            "🔔",
            "🧪",
            "🔐",
            "✏️",
            "🌐",
            "📜",
            "🖥",
            "📦",
            "📊",
            "🔎",
            "✅",
            "⛔",
            "🗑",
            "➖",
            "↩️",
            "‹",
            "×",
            "✓",
            "↻",
            "✨",
            "🎁",
        },
        key=len,
        reverse=True,
    )
)


def remove_decorative_prefix(text: str) -> str:
    # نشانگر وضعیت فروشگاه/کلید باید باقی بماند.
    if text.startswith(("🟢", "⚫", "🟡", "🔴")):
        return text
    clean = text.lstrip()
    for prefix in _DECORATIVE_PREFIXES:
        if clean.startswith(prefix):
            stripped = clean[len(prefix):].lstrip()
            return stripped or text
    return text


def resolve_button_style(button: InlineKeyboardButton, config: AppearanceConfig | None = None) -> str | None:
    config = config or _cache
    if config.button_theme == THEME_DEFAULT:
        return None
    if config.button_theme in {THEME_PRIMARY, THEME_SUCCESS, THEME_DANGER}:
        return None if button.callback_data == "noop" else config.button_theme
    return infer_button_role(button)


def style_button(button: InlineKeyboardButton, config: AppearanceConfig | None = None) -> InlineKeyboardButton:
    config = config or _cache
    payload: dict[str, Any] = button.model_dump(exclude_none=True)
    style = resolve_button_style(button, config)
    if style:
        payload["style"] = style
    else:
        payload.pop("style", None)

    slot = infer_emoji_slot(button)
    emoji_id = config.emoji_ids.get(slot or "") if config.premium_emoji_enabled else None
    if emoji_id:
        payload["icon_custom_emoji_id"] = emoji_id
        payload["text"] = remove_decorative_prefix(str(payload.get("text", "")))
    else:
        payload.pop("icon_custom_emoji_id", None)
    return InlineKeyboardButton(**payload)


def InlineKeyboardMarkup(*, inline_keyboard: list[list[InlineKeyboardButton]], **kwargs: Any) -> TelegramInlineKeyboardMarkup:
    """سازنده سازگار با aiogram که ظاهر سراسری را روی همه کلیدها اعمال می‌کند."""
    rows = [[style_button(button) for button in row] for row in inline_keyboard]
    return TelegramInlineKeyboardMarkup(inline_keyboard=rows, **kwargs)
