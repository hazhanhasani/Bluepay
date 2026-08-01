from __future__ import annotations

import ast
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
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

# تنظیمات گروهی نسخه‌های قبلی برای سازگاری نگه داشته می‌شوند. در رابط جدید،
# جایگزینی دقیق بر اساس خود ایموجی انجام می‌شود.
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
SETTING_EXACT_EMOJI_MAP = "ui_custom_emoji_map_v2"


@dataclass(slots=True)
class AppearanceConfig:
    button_theme: str = THEME_SEMANTIC
    premium_emoji_enabled: bool = False
    # نگاشت قدیمی گروه معنایی -> شناسه Custom Emoji
    emoji_ids: dict[str, str] = field(default_factory=dict)
    # نگاشت جدید و دقیق ایموجی عادی -> شناسه Custom Emoji
    exact_emoji_ids: dict[str, str] = field(default_factory=dict)


_cache = AppearanceConfig()


def get_appearance() -> AppearanceConfig:
    return _cache


def _as_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _normal_theme(value: str | None) -> str:
    return value if value in VALID_BUTTON_THEMES else THEME_SEMANTIC


def _normal_exact_map(raw: str | None) -> dict[str, str]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for normal_emoji, custom_id in value.items():
        emoji = str(normal_emoji or "").strip()
        identifier = str(custom_id or "").strip()
        if emoji and identifier.isdigit():
            result[emoji] = identifier
    return result


async def load_appearance_settings(session: AsyncSession) -> AppearanceConfig:
    global _cache
    theme = _normal_theme(await get_setting(session, SETTING_THEME, THEME_SEMANTIC))
    enabled = _as_bool(await get_setting(session, SETTING_PREMIUM_ENABLED, "0"))
    emoji_ids: dict[str, str] = {}
    for slot in EMOJI_SLOTS:
        value = (await get_setting(session, f"{SETTING_EMOJI_PREFIX}{slot}", "") or "").strip()
        if value:
            emoji_ids[slot] = value
    exact_emoji_ids = _normal_exact_map(await get_setting(session, SETTING_EXACT_EMOJI_MAP, "{}"))
    _cache = AppearanceConfig(
        button_theme=theme,
        premium_emoji_enabled=enabled,
        emoji_ids=emoji_ids,
        exact_emoji_ids=exact_emoji_ids,
    )
    return _cache


async def set_button_theme(session: AsyncSession, theme: str) -> AppearanceConfig:
    global _cache
    theme = _normal_theme(theme)
    await set_setting(session, SETTING_THEME, theme)
    _cache = AppearanceConfig(
        button_theme=theme,
        premium_emoji_enabled=_cache.premium_emoji_enabled,
        emoji_ids=dict(_cache.emoji_ids),
        exact_emoji_ids=dict(_cache.exact_emoji_ids),
    )
    return _cache


async def set_premium_emoji_enabled(session: AsyncSession, enabled: bool) -> AppearanceConfig:
    global _cache
    await set_setting(session, SETTING_PREMIUM_ENABLED, "1" if enabled else "0")
    _cache = AppearanceConfig(
        button_theme=_cache.button_theme,
        premium_emoji_enabled=bool(enabled),
        emoji_ids=dict(_cache.emoji_ids),
        exact_emoji_ids=dict(_cache.exact_emoji_ids),
    )
    return _cache


async def set_custom_emoji(session: AsyncSession, slot: str, emoji_id: str | None) -> AppearanceConfig:
    """API سازگار با تنظیمات گروهی نسخه 0.5.1 و قبل از آن."""
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
    _cache = AppearanceConfig(
        button_theme=_cache.button_theme,
        premium_emoji_enabled=_cache.premium_emoji_enabled,
        emoji_ids=ids,
        exact_emoji_ids=dict(_cache.exact_emoji_ids),
    )
    return _cache


async def set_exact_custom_emoji(
    session: AsyncSession,
    normal_emoji: str,
    emoji_id: str | None,
) -> AppearanceConfig:
    """نسخه پرمیوم یک ایموجی عادی را در کل ربات ثبت یا حذف می‌کند."""
    global _cache
    normal = (normal_emoji or "").strip()
    if not normal or normal not in dict(discover_bot_emojis()):
        raise ValueError("unknown bot emoji")
    clean = (emoji_id or "").strip()
    if clean and not clean.isdigit():
        raise ValueError("invalid custom emoji id")
    # اگر همین ایموجی از تنظیم گروهی نسخه قدیمی آمده باشد، آن نگاشت پاک می‌شود
    # تا انتخاب دقیق جدید روی دکمه‌های نامرتبط همان گروه اعمال نشود.
    legacy_ids = dict(_cache.emoji_ids)
    for slot, (_title, fallback) in EMOJI_SLOTS.items():
        if fallback == normal and slot in legacy_ids:
            await set_setting(session, f"{SETTING_EMOJI_PREFIX}{slot}", "")
            legacy_ids.pop(slot, None)

    exact = dict(_cache.exact_emoji_ids)
    if clean:
        exact[normal] = clean
    else:
        exact.pop(normal, None)
    await set_setting(session, SETTING_EXACT_EMOJI_MAP, json.dumps(exact, ensure_ascii=False, separators=(",", ":")))
    _cache = AppearanceConfig(
        button_theme=_cache.button_theme,
        premium_emoji_enabled=_cache.premium_emoji_enabled,
        emoji_ids=legacy_ids,
        exact_emoji_ids=exact,
    )
    return _cache


async def reset_appearance(session: AsyncSession) -> AppearanceConfig:
    global _cache
    await set_setting(session, SETTING_THEME, THEME_SEMANTIC)
    await set_setting(session, SETTING_PREMIUM_ENABLED, "0")
    await set_setting(session, SETTING_EXACT_EMOJI_MAP, "{}")
    for slot in EMOJI_SLOTS:
        await set_setting(session, f"{SETTING_EMOJI_PREFIX}{slot}", "")
    _cache = AppearanceConfig()
    return _cache


# نمادهایی که در رابط ربات به‌عنوان آیکون استفاده می‌شوند اما همه آن‌ها در
# استاندارد Unicode خاصیت Extended_Pictographic ندارند.
_EXPLICIT_UI_SYMBOLS = frozenset({"＋", "✓", "×", "‹", "›", "↻", "⌂", "•", "●", "○"})
_VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}
_ZWJ = "\u200d"
_KEYCAP = "\u20e3"


def _is_regional_indicator(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_skin_tone(ch: str) -> bool:
    return 0x1F3FB <= ord(ch) <= 0x1F3FF


def _is_emoji_base(ch: str) -> bool:
    code = ord(ch)
    return (
        ch in _EXPLICIT_UI_SYMBOLS
        or 0x1F000 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
        or 0x2B00 <= code <= 0x2BFF
        or 0x2300 <= code <= 0x23FF
        or 0x2190 <= code <= 0x21FF
    )


def extract_emoji_clusters(text: str) -> list[str]:
    """استخراج خوشه‌های ایموجی بدون وابستگی به کتابخانه جانبی."""
    result: list[str] = []
    index = 0
    while index < len(text):
        ch = text[index]

        # کلیدهای عددی مانند 1️⃣
        if ch in "#*0123456789":
            end = index + 1
            if end < len(text) and text[end] in _VARIATION_SELECTORS:
                end += 1
            if end < len(text) and text[end] == _KEYCAP:
                result.append(text[index : end + 1])
                index = end + 1
                continue

        # پرچم‌ها از دو Regional Indicator ساخته می‌شوند.
        if _is_regional_indicator(ch) and index + 1 < len(text) and _is_regional_indicator(text[index + 1]):
            result.append(text[index : index + 2])
            index += 2
            continue

        if not _is_emoji_base(ch):
            index += 1
            continue

        end = index + 1
        while end < len(text) and (text[end] in _VARIATION_SELECTORS or _is_skin_tone(text[end])):
            end += 1

        # پشتیبانی از خانواده‌ها و ترکیب‌های ZWJ.
        while end < len(text) and text[end] == _ZWJ:
            candidate = end + 1
            if candidate >= len(text) or not _is_emoji_base(text[candidate]):
                break
            end = candidate + 1
            while end < len(text) and (text[end] in _VARIATION_SELECTORS or _is_skin_tone(text[end])):
                end += 1

        cluster = text[index:end]
        # بعضی کاراکترهای محدوده نمادها، متن عادی هستند. وجود Presentation
        # ایموجی، نماد صریح رابط، یا دسته Symbol/Other آن‌ها را معتبر می‌کند.
        category = unicodedata.category(ch)
        if ch in _EXPLICIT_UI_SYMBOLS or "\ufe0f" in cluster or ord(ch) >= 0x1F000 or category in {"So", "Sk"}:
            result.append(cluster)
        index = end
    return result


def _iter_bot_string_literals() -> list[str]:
    app_root = Path(__file__).resolve().parents[1]
    sources = sorted((app_root / "bot").glob("*.py"))
    sources.extend(
        path
        for path in (
            app_root / "services" / "sms_notification_service.py",
            app_root / "services" / "appearance_service.py",
        )
        if path.exists()
    )
    values: list[str] = []
    for path in sources:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                values.append(node.value)
    return values


@lru_cache(maxsize=1)
def discover_bot_emojis() -> tuple[tuple[str, int], ...]:
    """فهرست تمام ایموجی‌ها و نمادهای رابط که واقعاً در کد ربات استفاده شده‌اند."""
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    order = 0
    for literal in _iter_bot_string_literals():
        for emoji in extract_emoji_clusters(literal):
            counts[emoji] += 1
            if emoji not in first_seen:
                first_seen[emoji] = order
                order += 1
    # پرتکرارها ابتدا می‌آیند و ترتیب ظهور، تساوی‌ها را پایدار نگه می‌دارد.
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], first_seen[item[0]])))


def emoji_token(emoji: str) -> str:
    return "-".join(f"{ord(ch):x}" for ch in emoji)


def emoji_from_token(token: str) -> str | None:
    try:
        value = "".join(chr(int(part, 16)) for part in token.split("-") if part)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value in dict(discover_bot_emojis()) else None


def effective_emoji_replacements(config: AppearanceConfig | None = None) -> dict[str, str]:
    config = config or _cache
    result: dict[str, str] = {}
    # تنظیمات گروهی قبلی فقط به ایموجی عادی همان گروه نگاشت می‌شوند.
    for slot, emoji_id in config.emoji_ids.items():
        fallback = EMOJI_SLOTS.get(slot, ("", ""))[1]
        if fallback and emoji_id:
            result[fallback] = emoji_id
    # تنظیم دقیق اولویت بالاتر دارد.
    result.update(config.exact_emoji_ids)
    return result


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
    """تشخیص گروه قدیمی؛ فقط برای سازگاری تنظیمات نسخه‌های قبلی."""
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
        {emoji for emoji, _count in discover_bot_emojis()},
        key=len,
        reverse=True,
    )
)


def remove_decorative_prefix(text: str) -> str:
    # در سازگاری گروهی قدیمی، نشانگر وضعیت باید باقی بماند. جایگزینی دقیق
    # ایموجی‌ها از remove_exact_emoji استفاده می‌کند و می‌تواند همین نشانگرها را عوض کند.
    if text.startswith(("🟢", "⚫", "🟡", "🔴")):
        return text
    clean = text.lstrip()
    for prefix in _DECORATIVE_PREFIXES:
        if clean.startswith(prefix):
            stripped = clean[len(prefix):].lstrip()
            return stripped or text
    return text


def remove_exact_emoji(text: str, emoji: str) -> str:
    replaced = text.replace(emoji, "", 1).strip()
    return re.sub(r"\s{2,}", " ", replaced) or text


def _button_exact_replacement(
    button: InlineKeyboardButton,
    config: AppearanceConfig,
) -> tuple[str, str] | None:
    text = button.text or ""
    replacements = effective_emoji_replacements(config)
    candidates: list[tuple[int, int, str, str]] = []
    for normal, custom_id in replacements.items():
        position = text.find(normal)
        if position >= 0:
            candidates.append((position, -len(normal), normal, custom_id))
    if not candidates:
        return None
    _position, _negative_length, normal, custom_id = min(candidates)
    return normal, custom_id


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

    # فهرست مدیریت باید همیشه خود ایموجی عادی را نشان دهد تا مدیر دقیقاً بداند
    # برای کدام کاراکتر در حال ثبت جایگزین است.
    is_catalog_button = (button.callback_data or "").startswith("admin:appearance:emoji:")
    replacement = (
        _button_exact_replacement(button, config)
        if config.premium_emoji_enabled and not is_catalog_button
        else None
    )
    if replacement:
        normal, emoji_id = replacement
        payload["icon_custom_emoji_id"] = emoji_id
        payload["text"] = remove_exact_emoji(str(payload.get("text", "")), normal)
    else:
        # سازگاری با تنظیمات قدیمی در صورتی که ایموجی عادی در متن دکمه وجود ندارد.
        slot = infer_emoji_slot(button)
        emoji_id = (
            config.emoji_ids.get(slot or "")
            if config.premium_emoji_enabled and not is_catalog_button
            else None
        )
        if emoji_id:
            payload["icon_custom_emoji_id"] = emoji_id
            payload["text"] = remove_decorative_prefix(str(payload.get("text", "")))
        else:
            payload.pop("icon_custom_emoji_id", None)
    return InlineKeyboardButton(**payload)


def premiumize_html(text: str, config: AppearanceConfig | None = None) -> str:
    """ایموجی‌های متن HTML ربات را با Custom Emoji دقیق آن‌ها جایگزین می‌کند."""
    config = config or _cache
    if not config.premium_emoji_enabled:
        return text
    replacements = effective_emoji_replacements(config)
    if not replacements:
        return text

    ordered = sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)
    parts = re.split(r"(<[^>]+>)", text)
    protected_tags = {"code", "pre", "tg-emoji"}
    protected_depth = 0
    rendered: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("<") and part.endswith(">"):
            match = re.match(r"<\s*(/?)\s*([a-zA-Z0-9-]+)", part)
            if match:
                closing, tag_name = match.groups()
                tag_name = tag_name.lower()
                if closing and tag_name in protected_tags:
                    protected_depth = max(0, protected_depth - 1)
                rendered.append(part)
                if not closing and tag_name in protected_tags and not part.rstrip().endswith("/>"):
                    protected_depth += 1
                continue
            rendered.append(part)
            continue
        if protected_depth:
            rendered.append(part)
            continue
        for normal, custom_id in ordered:
            part = part.replace(
                normal,
                f'<tg-emoji emoji-id="{custom_id}">{normal}</tg-emoji>',
            )
        rendered.append(part)
    return "".join(rendered)


def InlineKeyboardMarkup(*, inline_keyboard: list[list[InlineKeyboardButton]], **kwargs: Any) -> TelegramInlineKeyboardMarkup:
    """سازنده سازگار با aiogram که ظاهر سراسری را روی همه کلیدها اعمال می‌کند."""
    rows = [[style_button(button) for button in row] for row in inline_keyboard]
    return TelegramInlineKeyboardMarkup(inline_keyboard=rows, **kwargs)
