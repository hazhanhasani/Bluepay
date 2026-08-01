from aiogram.types import InlineKeyboardButton

from app.services.appearance_service import (
    AppearanceConfig,
    THEME_DEFAULT,
    THEME_SEMANTIC,
    infer_button_role,
    remove_decorative_prefix,
    style_button,
)


def test_semantic_button_roles():
    assert infer_button_role(InlineKeyboardButton(text="➕ ساخت فاکتور", callback_data="invoice:new")) == "success"
    assert infer_button_role(InlineKeyboardButton(text="🗑 حذف Callback", callback_data="callback:remove")) == "danger"
    assert infer_button_role(InlineKeyboardButton(text="💼 کیف پول", callback_data="wallet")) == "primary"
    assert infer_button_role(InlineKeyboardButton(text="اطلاعات", callback_data="noop")) is None


def test_premium_icon_replaces_decorative_emoji():
    config = AppearanceConfig(
        button_theme=THEME_SEMANTIC,
        premium_emoji_enabled=True,
        emoji_ids={"wallet": "5368324170671202286"},
    )
    button = style_button(InlineKeyboardButton(text="💼 کیف پول", callback_data="wallet"), config)
    assert button.icon_custom_emoji_id == "5368324170671202286"
    assert button.text == "کیف پول"
    assert button.style == "primary"


def test_default_theme_keeps_telegram_style():
    config = AppearanceConfig(button_theme=THEME_DEFAULT)
    button = style_button(InlineKeyboardButton(text="➕ ساخت", callback_data="invoice:new"), config)
    assert button.style is None


def test_status_marker_is_not_removed():
    assert remove_decorative_prefix("🟢 فروشگاه اصلی") == "🟢 فروشگاه اصلی"


def test_exact_emoji_replacement_has_priority():
    config = AppearanceConfig(
        button_theme=THEME_SEMANTIC,
        premium_emoji_enabled=True,
        emoji_ids={"card": "111"},
        exact_emoji_ids={"💳": "222"},
    )
    button = style_button(InlineKeyboardButton(text="💳 کارت‌های مقصد", callback_data="cards"), config)
    assert button.icon_custom_emoji_id == "222"
    assert button.text == "کارت‌های مقصد"


def test_discovered_catalog_contains_real_bot_emojis():
    from app.services.appearance_service import discover_bot_emojis, emoji_from_token, emoji_token

    catalog = dict(discover_bot_emojis())
    assert catalog["💳"] > 0
    assert catalog["🧾"] > 0
    assert emoji_from_token(emoji_token("💳")) == "💳"


def test_premium_html_skips_code_blocks():
    from app.services.appearance_service import premiumize_html

    config = AppearanceConfig(
        premium_emoji_enabled=True,
        exact_emoji_ids={"💳": "123456789"},
    )
    rendered = premiumize_html("💳 <b>کارت</b> <code>💳</code>", config)
    assert '<tg-emoji emoji-id="123456789">💳</tg-emoji>' in rendered
    assert "<code>💳</code>" in rendered
