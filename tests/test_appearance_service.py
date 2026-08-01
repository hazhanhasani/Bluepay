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
