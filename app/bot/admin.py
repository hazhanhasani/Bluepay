from __future__ import annotations

import html
import math
import secrets
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.types import InlineKeyboardMarkup as TelegramInlineKeyboardMarkup

from app.services.appearance_service import InlineKeyboardMarkup
from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload

from app.bot.keyboards import admin_menu, main_menu
from app.bot.states import AdminAppearanceEmojiState, AdminFeeState, AdminSmsApproveState, AdminWalletAdjustState
from app.bot.presentation import (
    badge,
    error,
    esc,
    fee_mode_label,
    info,
    invoice_status_label,
    money_toman,
    panel,
    sms_result_label,
    success,
    warning,
)
from app.core.config import settings
from app.db.session import SessionLocal
from app.models import BankCard, Invoice, Merchant, SmsTransaction, Store, StoreApiKey, UpdateLog, WalletLedger
from app.services.appearance_service import (
    THEME_LABELS,
    VALID_BUTTON_THEMES,
    discover_bot_emojis,
    effective_emoji_replacements,
    emoji_from_token,
    emoji_token,
    get_appearance,
    reset_appearance,
    set_button_theme,
    set_exact_custom_emoji,
    set_premium_emoji_enabled,
)
from app.services.callback_service import send_paid_callback
from app.services.integration_service import merchant_docs_url, merchant_sms_webhook_url
from app.services.invoice_service import confirm_invoice_paid, release_invoice_reservation
from app.services.settings_service import get_fee_defaults, set_fee_defaults
from app.services.storage_service import storage
from app.version import APP_VERSION

router = Router(name="admin")
PAGE_SIZE = 8
EMOJI_PAGE_SIZE = 24


def toman(value_rial: int | None) -> str:
    return f"{(value_rial or 0) // 10:,}"


def short(value: str | None, length: int = 22) -> str:
    value = (value or "-").strip()
    return value if len(value) <= length else value[: length - 1] + "…"


async def get_admin(user_id: int) -> Merchant | None:
    async with SessionLocal() as session:
        return await session.scalar(
            select(Merchant).where(Merchant.telegram_user_id == user_id, Merchant.is_admin.is_(True))
        )


async def require_admin_callback(callback: CallbackQuery) -> Merchant | None:
    admin = await get_admin(callback.from_user.id)
    if not admin:
        await callback.answer("دسترسی به این بخش فقط برای مدیر سامانه امکان‌پذیر است.", show_alert=True)
        return None
    return admin


async def require_admin_message(message: Message) -> Merchant | None:
    admin = await get_admin(message.from_user.id)
    if not admin:
        await message.answer("دسترسی به این بخش فقط برای مدیر سامانه امکان‌پذیر است.")
        return None
    return admin


def merchant_detail_keyboard(merchant: Merchant, page: int, self_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="➕ شارژ کیف پول", callback_data=f"admin:mcredit:{merchant.id}:{page}"),
            InlineKeyboardButton(text="➖ کسر از کیف پول", callback_data=f"admin:mdebit:{merchant.id}:{page}"),
        ],
        [InlineKeyboardButton(text="⚙️ تغییر مبلغ کارمزد", callback_data=f"admin:mfee:{merchant.id}:{page}")],
        [
            InlineKeyboardButton(text="🎁 کارمزد رایگان", callback_data=f"admin:mfeefree:{merchant.id}:{page}"),
            InlineKeyboardButton(text="🌐 استفاده از پیش‌فرض", callback_data=f"admin:mfeedefault:{merchant.id}:{page}"),
        ],
    ]
    if merchant.telegram_user_id != self_id:
        label = "✅ فعال‌کردن حساب" if not merchant.is_active else "⛔ غیرفعال‌کردن حساب"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"admin:mtoggle:{merchant.id}:{page}")])
    rows.extend(
        [
            [InlineKeyboardButton(text="🧾 فاکتورهای این پذیرنده", callback_data=f"admin:minvoices:{merchant.id}")],
            [InlineKeyboardButton(text="↩️ فهرست پذیرندگان", callback_data=f"admin:merchants:{page}")],
            [InlineKeyboardButton(text="👑 منوی مدیریت", callback_data="admin:panel")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def global_fee_defaults_keyboard(default_fee_rial: int, default_mode: str, *, confirm_apply: bool = False) -> InlineKeyboardMarkup:
    if confirm_apply:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ تأیید و اعمال به همه", callback_data="admin:gfee:apply:confirm")],
                [InlineKeyboardButton(text="انصراف", callback_data="admin:fee-defaults")],
            ]
        )

    def mode_label(mode: str, title: str) -> str:
        return f"✓ {title}" if default_mode == mode else title

    rows = [
        [InlineKeyboardButton(text="✏️ تغییر مبلغ پیش‌فرض", callback_data="admin:gfee:amount")],
        [
            InlineKeyboardButton(text="🎁 پیش‌فرض رایگان", callback_data="admin:gfee:free"),
            InlineKeyboardButton(
                text=f"مبلغ فعلی: {'رایگان' if default_fee_rial == 0 else toman(default_fee_rial) + ' تومان'}",
                callback_data="noop",
            ),
        ],
        [InlineKeyboardButton(text=mode_label("customer", "👤 پرداخت توسط مشتری"), callback_data="admin:gfee:mode:customer")],
        [InlineKeyboardButton(text=mode_label("split", "🤝 تقسیم مساوی"), callback_data="admin:gfee:mode:split")],
        [InlineKeyboardButton(text=mode_label("merchant", "🏪 پرداخت توسط پذیرنده"), callback_data="admin:gfee:mode:merchant")],
        [InlineKeyboardButton(text="🌐 اعمال این پیش‌فرض‌ها به همه پذیرندگان", callback_data="admin:gfee:apply")],
        [InlineKeyboardButton(text="↩️ مرکز مدیریت", callback_data="admin:panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoices_keyboard(invoices: list[Invoice], back_data: str = "admin:panel") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"#{item.id} • {invoice_status_label(item.status)} • {toman(item.payable_amount_rial)} ت", callback_data=f"admin:invoice:{item.id}")]
        for item in invoices
    ]
    rows.append([InlineKeyboardButton(text="↩️ بازگشت", callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)




def appearance_keyboard(*, confirm_reset: bool = False) -> InlineKeyboardMarkup:
    config = get_appearance()
    if confirm_reset:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ تأیید بازنشانی ظاهر", callback_data="admin:appearance:reset:confirm")],
                [InlineKeyboardButton(text="انصراف", callback_data="admin:appearance")],
            ]
        )

    def theme_title(theme: str) -> str:
        marker = "✓ " if config.button_theme == theme else ""
        return marker + THEME_LABELS[theme]

    premium_title = "خاموش‌کردن ایموجی پرمیوم" if config.premium_emoji_enabled else "فعال‌کردن ایموجی پرمیوم"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=theme_title("semantic"), callback_data="admin:appearance:theme:semantic")],
            [
                InlineKeyboardButton(text=theme_title("primary"), callback_data="admin:appearance:theme:primary"),
                InlineKeyboardButton(text=theme_title("success"), callback_data="admin:appearance:theme:success"),
            ],
            [
                InlineKeyboardButton(text=theme_title("danger"), callback_data="admin:appearance:theme:danger"),
                InlineKeyboardButton(text=theme_title("default"), callback_data="admin:appearance:theme:default"),
            ],
            [InlineKeyboardButton(text=f"✨ {premium_title}", callback_data="admin:appearance:premium:toggle")],
            [InlineKeyboardButton(text="🧩 فهرست کامل ایموجی‌ها", callback_data="admin:appearance:emojis:0")],
            [InlineKeyboardButton(text="🧪 پیش‌نمایش کلیدها", callback_data="admin:appearance:preview")],
            [InlineKeyboardButton(text="↻ بازنشانی ظاهر پیش‌فرض", callback_data="admin:appearance:reset")],
            [InlineKeyboardButton(text="↩️ مرکز مدیریت", callback_data="admin:panel")],
        ]
    )


def appearance_emoji_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    catalog = list(discover_bot_emojis())
    total_pages = max(1, math.ceil(len(catalog) / EMOJI_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * EMOJI_PAGE_SIZE
    visible = catalog[start : start + EMOJI_PAGE_SIZE]
    configured = effective_emoji_replacements()

    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(visible), 4):
        row: list[InlineKeyboardButton] = []
        for normal_emoji, usage_count in visible[index : index + 4]:
            marker = " ✓" if normal_emoji in configured else ""
            row.append(
                InlineKeyboardButton(
                    text=f"{normal_emoji} {usage_count}{marker}",
                    callback_data=f"admin:appearance:emoji:{emoji_token(normal_emoji)}:{page}",
                )
            )
        rows.append(row)

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹ قبلی", callback_data=f"admin:appearance:emojis:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1} از {total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="بعدی ›", callback_data=f"admin:appearance:emojis:{page + 1}"))
    rows.append(nav)
    rows.extend(
        [
            [InlineKeyboardButton(text="🧪 پیش‌نمایش کلیدها", callback_data="admin:appearance:preview")],
            [InlineKeyboardButton(text="↩️ تنظیمات ظاهر", callback_data="admin:appearance")],
            [InlineKeyboardButton(text="👑 مرکز مدیریت", callback_data="admin:panel")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_emoji_catalog(
    callback: CallbackQuery,
    *,
    page: int = 0,
    notice: str | None = None,
) -> None:
    catalog = list(discover_bot_emojis())
    total_pages = max(1, math.ceil(len(catalog) / EMOJI_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    catalog_set = {emoji for emoji, _count in catalog}
    configured = {
        emoji: custom_id
        for emoji, custom_id in effective_emoji_replacements().items()
        if emoji in catalog_set
    }
    lines = [
        f"ایموجی‌ها و نمادهای شناسایی‌شده در ربات: <b>{len(catalog):,}</b>",
        f"جایگزین‌های پرمیوم ثبت‌شده: <b>{len(configured):,}</b>",
        f"صفحه: <b>{page + 1:,} از {total_pages:,}</b>",
        "",
        "هر کلید، ایموجی عادی و تعداد استفاده آن در کد ربات را نشان می‌دهد.",
        "ایموجی موردنظر را انتخاب کنید و سپس نسخه پرمیوم همان ایموجی را ارسال کنید.",
        "علامت ✓ یعنی برای آن ایموجی جایگزین پرمیوم ثبت شده است.",
    ]
    if notice:
        lines.insert(0, f"✅ {esc(notice)}")
        lines.insert(1, "")
    await callback.message.edit_text(
        panel(
            "🧩",
            "فهرست کامل ایموجی‌های ربات",
            lines,
            subtitle="جایگزینی دقیق هر ایموجی عادی با نسخه Telegram Custom Emoji",
            footer="پس از ثبت، همان ایموجی در تمام کلیدها و متن‌های قالب‌بندی‌شده ربات جایگزین می‌شود.",
        ),
        reply_markup=appearance_emoji_keyboard(page),
    )


async def render_appearance_panel(callback: CallbackQuery, *, notice: str | None = None) -> None:
    config = get_appearance()
    catalog = list(discover_bot_emojis())
    catalog_set = {emoji for emoji, _count in catalog}
    configured = len([emoji for emoji in effective_emoji_replacements(config) if emoji in catalog_set])
    lines = [
        f"🎨 قالب رنگ کلیدها: <b>{esc(THEME_LABELS[config.button_theme])}</b>",
        f"✨ ایموجی پرمیوم: <b>{'فعال' if config.premium_emoji_enabled else 'غیرفعال'}</b>",
        f"🧩 ایموجی‌های جایگزین‌شده: <b>{configured:,} از {len(catalog):,}</b>",
        "",
        "در حالت هوشمند، عملیات تأییدی سبز، عملیات حساس قرمز و مسیرهای اصلی آبی نمایش داده می‌شوند.",
        "رنگ‌های قابل استفاده توسط تلگرام فقط آبی، سبز، قرمز و حالت پیش‌فرض هستند.",
    ]
    if notice:
        lines.insert(0, f"✅ {esc(notice)}")
        lines.insert(1, "")
    await callback.message.edit_text(
        panel(
            "🎨",
            "ظاهر و کلیدهای ربات",
            lines,
            subtitle="مدیریت رنگ دکمه‌ها و جایگزینی دقیق تمام ایموجی‌ها",
            footer="تغییرات بلافاصله روی منوها و متن‌های قالب‌بندی‌شده جدید اعمال می‌شوند.",
        ),
        reply_markup=appearance_keyboard(),
    )


@router.callback_query(F.data == "admin:appearance")
async def admin_appearance(callback: CallbackQuery, state: FSMContext):
    if not await require_admin_callback(callback):
        return
    await state.clear()
    await render_appearance_panel(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:appearance:theme:"))
async def admin_appearance_theme(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    theme = callback.data.rsplit(":", 1)[1]
    if theme not in VALID_BUTTON_THEMES:
        return await callback.answer("قالب انتخاب‌شده معتبر نیست.", show_alert=True)
    async with SessionLocal() as session:
        await set_button_theme(session, theme)
        await session.commit()
    await render_appearance_panel(callback, notice=f"قالب کلیدها روی «{THEME_LABELS[theme]}» قرار گرفت.")
    await callback.answer("قالب ذخیره شد.")


@router.callback_query(F.data == "admin:appearance:premium:toggle")
async def admin_appearance_premium_toggle(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    current = get_appearance()
    enabled = not current.premium_emoji_enabled
    if enabled and not effective_emoji_replacements(current):
        return await callback.answer(
            "ابتدا حداقل برای یکی از ایموجی‌های ربات، نسخه پرمیوم ثبت کنید.",
            show_alert=True,
        )
    async with SessionLocal() as session:
        await set_premium_emoji_enabled(session, enabled)
        await session.commit()
    await render_appearance_panel(callback, notice=f"ایموجی‌های پرمیوم {'فعال' if enabled else 'غیرفعال'} شدند.")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:appearance:emojis"))
async def admin_appearance_emojis(callback: CallbackQuery, state: FSMContext):
    if not await require_admin_callback(callback):
        return
    await state.clear()
    parts = (callback.data or "").split(":")
    try:
        page = int(parts[-1]) if parts[-1].isdigit() else 0
    except (TypeError, ValueError):
        page = 0
    await render_emoji_catalog(callback, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:appearance:emoji:"))
async def admin_appearance_emoji_start(callback: CallbackQuery, state: FSMContext):
    if not await require_admin_callback(callback):
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        return await callback.answer("شناسه ایموجی معتبر نیست.", show_alert=True)
    token = parts[-2]
    try:
        page = int(parts[-1])
    except (TypeError, ValueError):
        page = 0
    normal_emoji = emoji_from_token(token)
    if not normal_emoji:
        return await callback.answer("این ایموجی در فهرست فعلی ربات وجود ندارد.", show_alert=True)

    usage_count = dict(discover_bot_emojis()).get(normal_emoji, 0)
    current_id = effective_emoji_replacements().get(normal_emoji)
    await state.set_state(AdminAppearanceEmojiState.emoji)
    await state.update_data(appearance_emoji=normal_emoji, appearance_page=page)
    await callback.message.answer(
        panel(
            normal_emoji,
            f"جایگزین پرمیوم {normal_emoji}",
            [
                f"تعداد استفاده شناسایی‌شده در ربات: <b>{usage_count:,}</b>",
                "نسخه پرمیوم همین ایموجی را ارسال کنید؛ یا شناسه عددی Custom Emoji را بفرستید.",
                "برای حذف جایگزین فعلی و بازگشت به ایموجی عادی، عدد <code>0</code> را ارسال کنید.",
                "",
                f"شناسه فعلی: <code>{esc(current_id or 'تنظیم نشده')}</code>",
            ],
            subtitle="جایگزینی دقیق در تمام کلیدها و متن‌های قالب‌بندی‌شده",
            footer="پس از اعتبارسنجی تلگرام، حالت ایموجی پرمیوم به‌صورت خودکار فعال می‌شود.",
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="× لغو", callback_data=f"admin:appearance:emojis:{page}")]]
        ),
    )
    await callback.answer()


def extract_custom_emoji_id(message: Message) -> str | None:
    for entity in message.entities or []:
        if entity.type == "custom_emoji" and entity.custom_emoji_id:
            return entity.custom_emoji_id
    raw = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    return raw if raw.isdigit() else None


@router.message(AdminAppearanceEmojiState.emoji)
async def admin_appearance_emoji_save(message: Message, state: FSMContext):
    if not await require_admin_message(message):
        await state.clear()
        return
    data = await state.get_data()
    normal_emoji = str(data.get("appearance_emoji") or "")
    try:
        page = int(data.get("appearance_page") or 0)
    except (TypeError, ValueError):
        page = 0
    if normal_emoji not in dict(discover_bot_emojis()):
        await state.clear()
        return await message.answer("ایموجی انتخاب‌شده پیدا نشد؛ دوباره از فهرست کامل ایموجی‌ها اقدام کنید.")

    raw = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if raw == "0":
        async with SessionLocal() as session:
            await set_exact_custom_emoji(session, normal_emoji, None)
            await session.commit()
        await state.clear()
        return await message.answer(
            success(
                "جایگزین پرمیوم حذف شد",
                f"ایموجی <b>{esc(normal_emoji)}</b> دوباره در تمام بخش‌ها به‌صورت عادی نمایش داده می‌شود.",
            ),
            reply_markup=appearance_emoji_keyboard(page),
        )

    emoji_id = extract_custom_emoji_id(message)
    if not emoji_id:
        return await message.answer(
            error(
                "ایموجی قابل شناسایی نیست",
                f"نسخه پرمیوم ایموجی {esc(normal_emoji)} را ارسال کنید یا فقط شناسه عددی آن را بفرستید. برای حذف نیز عدد 0 را ارسال کنید.",
            )
        )

    try:
        stickers = await message.bot.get_custom_emoji_stickers(custom_emoji_ids=[emoji_id])
        if not stickers:
            return await message.answer(error("شناسه نامعتبر است", "تلگرام برای این شناسه Custom Emoji معتبری برنگرداند."))
        test_markup = TelegramInlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text=f"جایگزین پرمیوم {normal_emoji}",
                    icon_custom_emoji_id=emoji_id,
                    style="primary",
                    callback_data="noop",
                )
            ]]
        )
        await message.answer(
            f"🧪 پیش‌نمایش جایگزینی <b>{esc(normal_emoji)}</b> روی کلید:",
            reply_markup=test_markup,
        )
    except TelegramBadRequest as exc:
        return await message.answer(
            error(
                "تلگرام اجازه استفاده از این ایموجی را نداد",
                "شناسه را بررسی کنید. همچنین مالک ربات باید Telegram Premium داشته باشد یا ربات شرایط اعلام‌شده تلگرام برای Custom Emoji را داشته باشد."
                f"\n\n<code>{esc(str(exc))}</code>",
            )
        )

    async with SessionLocal() as session:
        await set_exact_custom_emoji(session, normal_emoji, emoji_id)
        await set_premium_emoji_enabled(session, True)
        await session.commit()
    await state.clear()
    await message.answer(
        success(
            "جایگزین پرمیوم ذخیره شد",
            f"از این پس ایموجی <b>{esc(normal_emoji)}</b> در تمام کلیدها و متن‌های قالب‌بندی‌شده با نسخه پرمیوم انتخابی نمایش داده می‌شود.",
        ),
        reply_markup=appearance_emoji_keyboard(page),
    )


@router.callback_query(F.data == "admin:appearance:preview")
async def admin_appearance_preview(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    preview = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ عملیات تأییدی", callback_data="admin:appearance:preview:success")],
            [InlineKeyboardButton(text="🔗 مسیر اصلی", callback_data="admin:appearance:preview:primary")],
            [InlineKeyboardButton(text="⛔ عملیات حساس", callback_data="admin:appearance:preview:danger")],
            [InlineKeyboardButton(text="⌂ صفحه اصلی", callback_data="home")],
        ]
    )
    # برای پیش‌نمایش دقیق، نقش‌ها از متن نیز تشخیص داده می‌شوند.
    await callback.message.answer(
        panel(
            "🧪",
            "پیش‌نمایش ظاهر کلیدها",
            ["رنگ و آیکون‌های زیر با تنظیمات فعلی سامانه ساخته شده‌اند."],
            subtitle="نمایش آزمایشی بدون تغییر عملیات",
        ),
        reply_markup=preview,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:appearance:preview:"))
async def admin_appearance_preview_noop(callback: CallbackQuery):
    await callback.answer("این کلید فقط برای پیش‌نمایش ظاهر است.")


@router.callback_query(F.data == "admin:appearance:reset")
async def admin_appearance_reset_ask(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.message.edit_text(
        warning(
            "بازنشانی تنظیمات ظاهر",
            "قالب رنگ به حالت هوشمند برمی‌گردد و تمام شناسه‌های ایموجی پرمیوم حذف می‌شوند. ادامه می‌دهید؟",
        ),
        reply_markup=appearance_keyboard(confirm_reset=True),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:appearance:reset:confirm")
async def admin_appearance_reset_confirm(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    async with SessionLocal() as session:
        await reset_appearance(session)
        await session.commit()
    await render_appearance_panel(callback, notice="تنظیمات ظاهر به حالت پیش‌فرض بازنشانی شد.")
    await callback.answer()


@router.message(Command("admin"))
async def admin_command(message: Message):
    if not await require_admin_message(message):
        return
    await message.answer(
        panel(
            "👑",
            "مرکز مدیریت بلوپی",
            [
                "مدیریت پذیرندگان، تراکنش‌ها، کیف پول‌ها و سلامت زیرساخت از این بخش انجام می‌شود.",
                "برای عملیات حساس، ابتدا جزئیات رکورد را بررسی و سپس اقدام کنید.",
            ],
            subtitle="کنترل متمرکز سامانه پرداخت",
        ),
        reply_markup=admin_menu(),
    )

@router.callback_query(F.data == "admin:panel")
async def admin_panel(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.message.edit_text(
        panel(
            "👑",
            "مرکز مدیریت بلوپی",
            [
                "• پایش وضعیت کسب‌وکار و تراکنش‌ها",
                "• مدیریت پذیرندگان، کیف پول و پیش‌فرض‌های کارمزد",
                "• تنظیم رنگ کلیدها و ایموجی‌های پرمیوم ربات",
                "• بررسی پیامک‌های بانکی و تطبیق دستی",
                "• کنترل نسخه، پشتیبان‌گیری و سلامت سرویس",
            ],
            subtitle="داشبورد عملیاتی مدیر سامانه",
            footer="یکی از بخش‌های مدیریتی را انتخاب کنید.",
        ),
        reply_markup=admin_menu(),
    )
    await callback.answer()

async def render_global_fee_defaults(callback: CallbackQuery, *, notice: str | None = None) -> None:
    async with SessionLocal() as session:
        default_fee_rial, default_mode = await get_fee_defaults(session)
        total_merchants = await session.scalar(select(func.count(Merchant.id))) or 0
        matching_merchants = await session.scalar(
            select(func.count(Merchant.id)).where(
                Merchant.verification_fee_rial == default_fee_rial,
                Merchant.fee_mode == default_mode,
            )
        ) or 0

    lines = [
        f"💰 مبلغ پیش‌فرض هر تأیید: <b>{'رایگان' if default_fee_rial == 0 else money_toman(default_fee_rial)}</b>",
        f"⚙️ روش پیش‌فرض پرداخت هزینه: <b>{esc(fee_mode_label(default_mode))}</b>",
        "",
        f"👥 پذیرندگان مطابق پیش‌فرض: <b>{matching_merchants:,} از {total_merchants:,}</b>",
        "پذیرنده‌های جدید به‌صورت خودکار با این تنظیمات ساخته می‌شوند.",
        "تغییر پیش‌فرض، تنظیم اختصاصی پذیرندگان فعلی را تغییر نمی‌دهد؛ مگر گزینه اعمال همگانی را انتخاب کنید.",
    ]
    if notice:
        lines.insert(0, f"✅ {esc(notice)}")
        lines.insert(1, "")
    await callback.message.edit_text(
        panel(
            "⚙️",
            "پیش‌فرض‌های کارمزد",
            lines,
            subtitle="مدیریت سیاست عمومی صدور فاکتور",
            footer="مبلغ صفر به‌معنای سرویس رایگان و بدون نیاز به اعتبار کیف پول است.",
        ),
        reply_markup=global_fee_defaults_keyboard(default_fee_rial, default_mode),
    )


@router.callback_query(F.data == "admin:fee-defaults")
async def admin_fee_defaults(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await render_global_fee_defaults(callback)
    await callback.answer()


@router.callback_query(F.data == "admin:gfee:amount")
async def admin_global_fee_amount_start(callback: CallbackQuery, state: FSMContext):
    if not await require_admin_callback(callback):
        return
    await state.set_state(AdminFeeState.amount)
    await state.update_data(scope="global")
    await callback.message.answer(
        panel(
            "✏️",
            "تغییر مبلغ پیش‌فرض",
            [
                "مبلغ کارمزد هر تأیید را به تومان و فقط به‌صورت عدد وارد کنید.",
                "برای رایگان‌کردن سرویس، عدد <code>0</code> را ارسال کنید.",
            ],
            subtitle="این مبلغ برای پذیرنده‌های جدید استفاده می‌شود",
        )
    )
    await callback.answer()


@router.callback_query(F.data == "admin:gfee:free")
async def admin_global_fee_free(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    async with SessionLocal() as session:
        await set_fee_defaults(session, verification_fee_rial=0)
        await session.commit()
    await render_global_fee_defaults(callback, notice="کارمزد پیش‌فرض روی حالت رایگان قرار گرفت.")
    await callback.answer("پیش‌فرض رایگان شد.")


@router.callback_query(F.data.startswith("admin:gfee:mode:"))
async def admin_global_fee_mode(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    mode = callback.data.rsplit(":", 1)[1]
    if mode not in {"customer", "split", "merchant"}:
        return await callback.answer("روش کارمزد معتبر نیست.", show_alert=True)
    async with SessionLocal() as session:
        await set_fee_defaults(session, fee_mode=mode)
        await session.commit()
    await render_global_fee_defaults(callback, notice="روش پیش‌فرض پرداخت کارمزد به‌روزرسانی شد.")
    await callback.answer("پیش‌فرض ذخیره شد.")


@router.callback_query(F.data == "admin:gfee:apply")
async def admin_global_fee_apply_confirm(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    async with SessionLocal() as session:
        default_fee_rial, default_mode = await get_fee_defaults(session)
        total_merchants = await session.scalar(select(func.count(Merchant.id))) or 0
    await callback.message.edit_text(
        warning(
            "تأیید اعمال همگانی",
            "\n".join(
                [
                    f"این عملیات تنظیمات کارمزد <b>{total_merchants:,}</b> پذیرنده را بازنویسی می‌کند.",
                    f"مبلغ جدید: <b>{'رایگان' if default_fee_rial == 0 else money_toman(default_fee_rial)}</b>",
                    f"روش جدید: <b>{esc(fee_mode_label(default_mode))}</b>",
                    "تنظیمات اختصاصی فعلی پذیرندگان نیز جایگزین خواهد شد.",
                ]
            ),
            footer="این عملیات فقط روی فاکتورهای جدید اثر می‌گذارد و فاکتورهای قبلی تغییر نمی‌کنند.",
        ),
        reply_markup=global_fee_defaults_keyboard(default_fee_rial, default_mode, confirm_apply=True),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:gfee:apply:confirm")
async def admin_global_fee_apply(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    async with SessionLocal() as session:
        default_fee_rial, default_mode = await get_fee_defaults(session)
        total_merchants = await session.scalar(select(func.count(Merchant.id))) or 0
        await session.execute(
            update(Merchant).values(
                verification_fee_rial=default_fee_rial,
                fee_mode=default_mode,
            )
        )
        await session.commit()
        storage.mark_dirty()
    await render_global_fee_defaults(
        callback,
        notice=f"پیش‌فرض‌های کارمزد برای {total_merchants:,} پذیرنده اعمال شد.",
    )
    await callback.answer("تنظیمات همگانی اعمال شد.")


@router.callback_query(F.data == "admin:dashboard")
async def admin_dashboard(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    async with SessionLocal() as session:
        merchants = await session.scalar(select(func.count(Merchant.id))) or 0
        active_merchants = await session.scalar(select(func.count(Merchant.id)).where(Merchant.is_active.is_(True))) or 0
        cards = await session.scalar(select(func.count(BankCard.id))) or 0
        invoices = await session.scalar(select(func.count(Invoice.id))) or 0
        pending = await session.scalar(select(func.count(Invoice.id)).where(Invoice.status == "pending")) or 0
        paid = await session.scalar(select(func.count(Invoice.id)).where(Invoice.status == "paid")) or 0
        review = await session.scalar(
            select(func.count(SmsTransaction.id)).where(SmsTransaction.status.in_(["review", "unmatched"]))
        ) or 0
        wallet_total = await session.scalar(select(func.coalesce(func.sum(Merchant.wallet_balance_rial), 0))) or 0
        reserved_total = await session.scalar(select(func.coalesce(func.sum(Merchant.reserved_balance_rial), 0))) or 0
        paid_total = await session.scalar(
            select(func.coalesce(func.sum(Invoice.payable_amount_rial), 0)).where(Invoice.status == "paid")
        ) or 0

    conversion = round((paid / invoices) * 100, 1) if invoices else 0
    text = panel(
        "📊",
        "نمای مدیریتی سامانه",
        [
            "<b>پذیرندگان و زیرساخت</b>",
            f"👥 کل پذیرندگان: <b>{merchants:,}</b>",
            f"🟢 حساب‌های فعال: <b>{active_merchants:,}</b>",
            f"💳 کارت‌های ثبت‌شده: <b>{cards:,}</b>",
            "",
            "<b>عملکرد پرداخت</b>",
            f"🧾 کل فاکتورها: <b>{invoices:,}</b>",
            f"🕓 در انتظار پرداخت: <b>{pending:,}</b>",
            f"✅ پرداخت‌های موفق: <b>{paid:,}</b>",
            f"📈 نرخ تبدیل: <b>{conversion}%</b>",
            f"🔎 نیازمند بررسی: <b>{review:,}</b>",
            "",
            "<b>شاخص‌های مالی</b>",
            f"💼 مجموع اعتبار کیف پول: <b>{money_toman(wallet_total)}</b>",
            f"🔒 اعتبار رزروشده: <b>{money_toman(reserved_total)}</b>",
            f"💰 حجم پرداخت تأییدشده: <b>{money_toman(paid_total)}</b>",
        ],
        subtitle="نمای لحظه‌ای عملیات و سلامت کسب‌وکار",
        footer="آمار بر پایه داده‌های ثبت‌شده در پایگاه داده فعلی محاسبه شده است.",
    )
    await callback.message.edit_text(text, reply_markup=admin_menu())
    await callback.answer()

@router.callback_query(F.data.startswith("admin:merchants:"))
async def admin_merchants(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    try:
        page = max(0, int(callback.data.rsplit(":", 1)[1]))
    except ValueError:
        page = 0
    async with SessionLocal() as session:
        total = await session.scalar(select(func.count(Merchant.id))) or 0
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(page, pages - 1)
        merchants = list(
            (
                await session.scalars(
                    select(Merchant).order_by(Merchant.id.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE)
                )
            ).all()
        )
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'🟢' if item.is_active else '🔴'} {short(item.name, 18)}  •  BP-{item.id:06d}",
                callback_data=f"admin:merchant:{item.id}:{page}",
            )
        ]
        for item in merchants
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹ قبلی", callback_data=f"admin:merchants:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="بعدی ›", callback_data=f"admin:merchants:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="‹ مرکز مدیریت", callback_data="admin:panel")])
    await callback.message.edit_text(
        panel(
            "👥",
            "مدیریت پذیرندگان",
            [
                f"مجموع حساب‌ها: <b>{total:,}</b>",
                f"صفحه جاری: <b>{page + 1} از {pages}</b>",
                "برای مشاهده وضعیت مالی و تنظیمات هر پذیرنده، حساب را انتخاب کنید.",
            ],
            subtitle="حساب‌ها، اعتبار و دسترسی سرویس",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin:merchant:"))
async def admin_merchant_detail(callback: CallbackQuery):
    admin = await require_admin_callback(callback)
    if not admin:
        return
    parts = callback.data.split(":")
    merchant_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    async with SessionLocal() as session:
        merchant = await session.get(Merchant, merchant_id)
        if not merchant:
            return await callback.answer("پذیرنده یافت نشد.", show_alert=True)
        cards_count = await session.scalar(select(func.count(BankCard.id)).where(BankCard.merchant_id == merchant.id)) or 0
        invoice_count = await session.scalar(select(func.count(Invoice.id)).where(Invoice.merchant_id == merchant.id)) or 0
        paid_count = await session.scalar(
            select(func.count(Invoice.id)).where(Invoice.merchant_id == merchant.id, Invoice.status == "paid")
        ) or 0
        store_count = await session.scalar(
            select(func.count(Store.id)).where(Store.merchant_id == merchant.id)
        ) or 0
        active_key_count = await session.scalar(
            select(func.count(StoreApiKey.id))
            .join(Store, Store.id == StoreApiKey.store_id)
            .where(Store.merchant_id == merchant.id, StoreApiKey.is_active.is_(True))
        ) or 0
        default_fee_rial, default_fee_mode = await get_fee_defaults(session)
    fee_source = (
        "همگانی (مطابق پیش‌فرض)"
        if merchant.verification_fee_rial == default_fee_rial and merchant.fee_mode == default_fee_mode
        else "اختصاصی این پذیرنده"
    )
    text = panel(
        "👤",
        "پرونده پذیرنده",
        [
            f"🪪 شناسه پذیرنده: <code>BP-{merchant.id:06d}</code>",
            f"💬 Telegram ID: <code>{merchant.telegram_user_id}</code>",
            f"🏷 نام حساب: <b>{esc(merchant.name)}</b>",
            f"📡 وضعیت: <b>{badge('active' if merchant.is_active else 'inactive')}</b>",
            f"🛡 سطح دسترسی: <b>{'مدیر سامانه' if merchant.is_admin else 'پذیرنده'}</b>",
            "",
            "<b>وضعیت مالی</b>",
            f"💼 موجودی کل: <b>{money_toman(merchant.wallet_balance_rial)}</b>",
            f"🔒 رزروشده: <b>{money_toman(merchant.reserved_balance_rial)}</b>",
            f"✅ قابل استفاده: <b>{money_toman(merchant.available_balance_rial)}</b>",
            f"🧾 هزینه هر تأیید: <b>{'رایگان' if merchant.verification_fee_rial == 0 else money_toman(merchant.verification_fee_rial)}</b>",
            f"⚙️ مدل کارمزد: <b>{esc(fee_mode_label(merchant.fee_mode))}</b>",
            f"🌐 منبع تنظیم: <b>{fee_source}</b>",
            "",
            "<b>وضعیت اتصال</b>",
            f"🏪 فروشگاه‌ها: <b>{store_count:,}</b>",
            f"🔑 کلیدهای API فعال: <b>{active_key_count:,}</b>",
            f"🔔 Callback عمومی: <code>{esc(short(merchant.callback_url, 46))}</code>",
            f"📲 وبهوک پیامک:\n<code>{esc(merchant_sms_webhook_url(merchant))}</code>",
            f"📚 مستندات عمومی API:\n<code>{esc(merchant_docs_url(merchant))}</code>",
            "",
            f"📊 کارت‌ها: <b>{cards_count:,}</b>  •  فاکتورها: <b>{invoice_count:,}</b>  •  موفق: <b>{paid_count:,}</b>",
        ],
        subtitle="اطلاعات عملیاتی و مالی حساب",
    )
    await callback.message.edit_text(text, reply_markup=merchant_detail_keyboard(merchant, page, callback.from_user.id))
    await callback.answer()

@router.callback_query(F.data.startswith("admin:mtoggle:"))
async def admin_toggle_merchant(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    _, _, merchant_id, page = callback.data.split(":")
    async with SessionLocal() as session:
        merchant = await session.get(Merchant, int(merchant_id))
        if not merchant:
            return await callback.answer("پذیرنده پیدا نشد.", show_alert=True)
        if merchant.telegram_user_id == callback.from_user.id:
            return await callback.answer("حساب مدیر اصلی سامانه قابل غیرفعال‌سازی نیست.", show_alert=True)
        merchant.is_active = not merchant.is_active
        await session.commit()
    status = "فعال" if merchant.is_active else "غیرفعال"
    await callback.message.edit_text(
        f"✅ وضعیت حساب روی <b>{status}</b> تنظیم شد.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="بازگشت به پذیرنده", callback_data=f"admin:merchant:{merchant.id}:{page}")],
                [InlineKeyboardButton(text="👑 منوی مدیریت", callback_data="admin:panel")],
            ]
        ),
    )
    await callback.answer()


async def start_wallet_adjust(callback: CallbackQuery, state: FSMContext, action: str):
    if not await require_admin_callback(callback):
        return
    _, _, merchant_id, page = callback.data.split(":")
    await state.set_state(AdminWalletAdjustState.amount)
    await state.update_data(merchant_id=int(merchant_id), page=int(page), action=action)
    label = "شارژ" if action == "credit" else "کسر"
    await callback.message.answer(f"مبلغ موردنظر برای {label} کیف پول را به تومان و فقط به‌صورت عدد وارد کنید:")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:mcredit:"))
async def admin_credit_start(callback: CallbackQuery, state: FSMContext):
    await start_wallet_adjust(callback, state, "credit")


@router.callback_query(F.data.startswith("admin:mdebit:"))
async def admin_debit_start(callback: CallbackQuery, state: FSMContext):
    await start_wallet_adjust(callback, state, "debit")


@router.message(AdminWalletAdjustState.amount)
async def admin_wallet_adjust_amount(message: Message, state: FSMContext):
    admin = await require_admin_message(message)
    if not admin:
        await state.clear()
        return
    raw = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if not raw or int(raw) <= 0:
        return await message.answer("مبلغ باید عددی و بیشتر از صفر باشد.")
    amount_rial = int(raw) * 10
    data = await state.get_data()
    async with SessionLocal() as session:
        merchant = await session.get(Merchant, data["merchant_id"])
        if not merchant:
            await state.clear()
            return await message.answer("پذیرنده پیدا نشد.")
        signed = amount_rial if data["action"] == "credit" else -amount_rial
        if signed < 0 and merchant.wallet_balance_rial + signed < merchant.reserved_balance_rial:
            return await message.answer("این کسر باعث می‌شود موجودی از مبلغ رزروشده کمتر شود؛ مبلغ کمتری وارد کنید.")
        merchant.wallet_balance_rial += signed
        session.add(
            WalletLedger(
                merchant_id=merchant.id,
                entry_type="admin_credit" if signed > 0 else "admin_debit",
                amount_rial=signed,
                balance_after_rial=merchant.wallet_balance_rial,
                description=f"اصلاح کیف پول توسط مدیر {admin.telegram_user_id}",
                idempotency_key=f"admin-adjust:{message.message_id}:{merchant.id}:{secrets.token_hex(4)}",
            )
        )
        await session.commit()
    await state.clear()
    action_label = "شارژ" if signed > 0 else "کسر"
    await message.answer(
        f"✅ {action_label} انجام شد.\nموجودی جدید: <b>{toman(merchant.wallet_balance_rial)} تومان</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="بازگشت به پذیرنده", callback_data=f"admin:merchant:{merchant.id}:{data['page']}")],
                [InlineKeyboardButton(text="👑 منوی مدیریت", callback_data="admin:panel")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("admin:mfeedefault:"))
async def admin_fee_use_default(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    _, _, merchant_id, page = callback.data.split(":")
    async with SessionLocal() as session:
        merchant = await session.get(Merchant, int(merchant_id))
        if not merchant:
            return await callback.answer("پذیرنده یافت نشد.", show_alert=True)
        default_fee_rial, default_fee_mode = await get_fee_defaults(session)
        merchant.verification_fee_rial = default_fee_rial
        merchant.fee_mode = default_fee_mode
        await session.commit()
    await callback.message.edit_text(
        success(
            "پیش‌فرض همگانی اعمال شد",
            "مبلغ و روش کارمزد این پذیرنده با تنظیمات عمومی سامانه هماهنگ شد.",
        ),
        reply_markup=merchant_detail_keyboard(merchant, int(page), callback.from_user.id),
    )
    await callback.answer("تنظیمات پیش‌فرض اعمال شد.")


@router.callback_query(F.data.startswith("admin:mfeefree:"))
async def admin_fee_free(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    _, _, merchant_id, page = callback.data.split(":")
    async with SessionLocal() as session:
        merchant = await session.get(Merchant, int(merchant_id))
        if not merchant:
            return await callback.answer("پذیرنده یافت نشد.", show_alert=True)
        merchant.verification_fee_rial = 0
        await session.commit()
    await callback.message.edit_text(
        success(
            "کارمزد غیرفعال شد",
            "از این پس فاکتورهای جدید این پذیرنده بدون هزینه تأیید صادر می‌شوند و به موجودی کیف پول نیاز ندارند.",
        ),
        reply_markup=merchant_detail_keyboard(merchant, int(page), callback.from_user.id),
    )
    await callback.answer("کارمزد رایگان شد.")


@router.callback_query(F.data.startswith("admin:mfee:"))
async def admin_fee_start(callback: CallbackQuery, state: FSMContext):
    if not await require_admin_callback(callback):
        return
    _, _, merchant_id, page = callback.data.split(":")
    await state.set_state(AdminFeeState.amount)
    await state.update_data(scope="merchant", merchant_id=int(merchant_id), page=int(page))
    await callback.message.answer(
        "مبلغ کارمزد هر تأیید را به تومان وارد کنید. برای غیرفعال‌کردن کارمزد و ارائه سرویس رایگان، عدد <code>0</code> را ارسال کنید:"
    )
    await callback.answer()


@router.message(AdminFeeState.amount)
async def admin_fee_amount(message: Message, state: FSMContext):
    if not await require_admin_message(message):
        await state.clear()
        return
    raw = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if not raw:
        return await message.answer("مبلغ کارمزد باید عددی باشد؛ عدد صفر یعنی کارمزد رایگان.")

    amount_toman = int(raw)
    amount_rial = amount_toman * 10
    data = await state.get_data()
    scope = data.get("scope", "merchant")

    if scope == "global":
        async with SessionLocal() as session:
            default_fee_rial, default_mode = await set_fee_defaults(
                session,
                verification_fee_rial=amount_rial,
            )
            await session.commit()
        await state.clear()
        fee_label = "رایگان (غیرفعال)" if amount_toman == 0 else f"{amount_toman:,} تومان"
        await message.answer(
            success(
                "مبلغ پیش‌فرض ذخیره شد",
                f"مبلغ کارمزد پیش‌فرض روی <b>{fee_label}</b> تنظیم شد. این مقدار برای پذیرنده‌های جدید استفاده می‌شود.",
            ),
            reply_markup=global_fee_defaults_keyboard(default_fee_rial, default_mode),
        )
        return

    async with SessionLocal() as session:
        merchant = await session.get(Merchant, data["merchant_id"])
        if not merchant:
            await state.clear()
            return await message.answer("پذیرنده پیدا نشد.")
        merchant.verification_fee_rial = amount_rial
        await session.commit()
    await state.clear()
    fee_label = "رایگان (غیرفعال)" if amount_toman == 0 else f"{amount_toman:,} تومان"
    await message.answer(
        f"✅ کارمزد روی <b>{fee_label}</b> تنظیم شد.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="بازگشت به پذیرنده", callback_data=f"admin:merchant:{merchant.id}:{data['page']}")]
            ]
        ),
    )


@router.callback_query(F.data == "admin:invoices")
async def admin_invoices(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    async with SessionLocal() as session:
        invoices = list((await session.scalars(select(Invoice).order_by(Invoice.id.desc()).limit(12))).all())
    text = panel(
        "🧾",
        "فاکتورهای اخیر",
        [
            f"تعداد نمایش‌داده‌شده: <b>{len(invoices)}</b>",
            "برای مشاهده مبلغ، پذیرنده، کارت مقصد و وضعیت نهایی، یک فاکتور را انتخاب کنید.",
        ],
        subtitle="آخرین درخواست‌های پرداخت سامانه",
    )
    await callback.message.edit_text(text, reply_markup=invoices_keyboard(invoices))
    await callback.answer()

@router.callback_query(F.data.startswith("admin:minvoices:"))
async def admin_merchant_invoices(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    merchant_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        invoices = list(
            (
                await session.scalars(
                    select(Invoice).where(Invoice.merchant_id == merchant_id).order_by(Invoice.id.desc()).limit(12)
                )
            ).all()
        )
    await callback.message.edit_text(
        f"🧾 <b>فاکتورهای پذیرنده #{merchant_id}</b>",
        reply_markup=invoices_keyboard(invoices, f"admin:merchant:{merchant_id}:0"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:invoice:"))
async def admin_invoice_detail(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    invoice_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        invoice = await session.get(Invoice, invoice_id)
        if not invoice:
            return await callback.answer("فاکتور یافت نشد.", show_alert=True)
        merchant = await session.get(Merchant, invoice.merchant_id)
        card = await session.get(BankCard, invoice.card_id)
    text = panel(
        "🧾",
        "جزئیات فاکتور",
        [
            f"🪪 شناسه داخلی: <code>{invoice.id}</code>",
            f"💳 Payment ID: <code>{invoice.token}</code>",
            f"📦 Order ID: <code>{esc(invoice.order_id)}</code>",
            f"👤 پذیرنده: <b>{esc(merchant.name if merchant else '-')}</b>",
            f"📡 وضعیت: <b>{invoice_status_label(invoice.status)}</b>",
            "",
            f"💵 مبلغ پایه: <b>{money_toman(invoice.base_amount_rial)}</b>",
            f"🧾 هزینه تأیید: <b>{money_toman(invoice.fee_amount_rial)}</b>",
            f"🔢 کد تطبیق: <b>+{money_toman(invoice.unique_amount_rial)}</b>",
            f"💰 مبلغ نهایی مشتری: <b>{money_toman(invoice.payable_amount_rial)}</b>",
            "",
            f"🏦 کارت مقصد: <b>{esc(card.bank_code if card else '-')} •••• {card.card_last4 if card else '-'}</b>",
            f"🔖 شماره مرجع: <code>{esc(invoice.reference_number or '-')}</code>",
        ],
        subtitle="اطلاعات مالی و وضعیت تراکنش",
    )
    rows = []
    if invoice.status == "pending":
        rows.append([InlineKeyboardButton(text="× لغو فاکتور", callback_data=f"admin:invoicecancel:{invoice.id}")])
    rows.extend([
        [InlineKeyboardButton(text="‹ فهرست فاکتورها", callback_data="admin:invoices")],
        [InlineKeyboardButton(text="👑 مرکز مدیریت", callback_data="admin:panel")],
    ])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

@router.callback_query(F.data.startswith("admin:invoicecancel:"))
async def admin_invoice_cancel(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    invoice_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        invoice = await session.get(Invoice, invoice_id)
        if not invoice:
            return await callback.answer("فاکتور پیدا نشد.", show_alert=True)
        if invoice.status != "pending":
            return await callback.answer(f"وضعیت فاکتور {invoice.status} است.", show_alert=True)
        await release_invoice_reservation(session, invoice, "cancelled")
        await session.commit()
    await callback.message.edit_text(
        "✅ فاکتور لغو شد و مبلغ رزروشده به موجودی قابل استفاده بازگشت.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="مشاهده فاکتور", callback_data=f"admin:invoice:{invoice_id}")],
                [InlineKeyboardButton(text="🧾 فهرست فاکتورها", callback_data="admin:invoices")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:reviews")
async def admin_reviews(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    async with SessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(SmsTransaction)
                    .where(SmsTransaction.status.in_(["review", "unmatched"]))
                    .order_by(SmsTransaction.id.desc())
                    .limit(12)
                )
            ).all()
        )
    keyboard_rows = [
        [InlineKeyboardButton(text=f"#{row.id} • {row.bank_code} • {toman(row.amount_rial)} ت", callback_data=f"admin:sms:{row.id}")]
        for row in rows
    ]
    keyboard_rows.append([InlineKeyboardButton(text="‹ مرکز مدیریت", callback_data="admin:panel")])
    text = panel(
        "🔎",
        "صف بررسی تراکنش‌ها",
        (
            f"<b>{len(rows)}</b> پیامک برای بررسی دستی آماده است. هر مورد را فقط پس از تطبیق مبلغ، بانک و کارت تأیید کنید."
            if rows else "در حال حاضر هیچ پیامکی نیازمند بررسی دستی نیست."
        ),
        subtitle="پیامک‌های مبهم یا بدون فاکتور منطبق",
        footer="تأیید دستی یک پیامک، فاکتور را قطعی و Callback را فعال می‌کند.",
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows))
    await callback.answer()

@router.callback_query(lambda query: bool(query.data and query.data.startswith("admin:sms:") and query.data.count(":") == 2))
async def admin_sms_detail(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    sms_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        sms = await session.get(SmsTransaction, sms_id)
    if not sms:
        return await callback.answer("پیامک یافت نشد.", show_alert=True)
    text = panel(
        "📨",
        "جزئیات پیامک بانکی",
        [
            f"🪪 شناسه: <code>{sms.id}</code>",
            f"🏦 بانک: <b>{esc(sms.bank_code)}</b>",
            f"💵 مبلغ: <b>{money_toman(sms.amount_rial)}</b>",
            f"💳 کارت: <code>•••• {sms.card_last4 or '-'}</code>",
            f"🔖 مرجع: <code>{esc(sms.reference_number or '-')}</code>",
            f"📡 وضعیت: <b>{sms_result_label(sms.status)}</b>",
            f"🧠 اطمینان تشخیص: <b>{sms.parse_confidence}%</b>",
            "",
            "<b>متن پیامک</b>",
            f"<blockquote>{esc(sms.raw_message[:700])}</blockquote>",
        ],
        subtitle="داده خام و نتیجه تحلیل موتور تطبیق",
        footer="پیش از اتصال دستی، مبلغ و کارت مقصد فاکتور را دقیق بررسی کنید.",
    )
    rows = [
        [InlineKeyboardButton(text="✓ اتصال به فاکتور", callback_data=f"admin:sms:approve:{sms.id}")],
        [InlineKeyboardButton(text="× رد پیامک", callback_data=f"admin:sms:reject:{sms.id}")],
        [InlineKeyboardButton(text="‹ صف بررسی", callback_data="admin:reviews")],
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

@router.callback_query(F.data.startswith("admin:sms:reject:"))
async def admin_sms_reject(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    sms_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        sms = await session.get(SmsTransaction, sms_id)
        if not sms:
            return await callback.answer("پیامک پیدا نشد.", show_alert=True)
        if sms.status == "matched":
            return await callback.answer("پیامک تأییدشده قابل رد نیست.", show_alert=True)
        sms.status = "rejected"
        await session.commit()
    await callback.message.edit_text("✅ پیامک از فرایند بررسی خارج شد.", reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("admin:sms:approve:"))
async def admin_sms_approve_start(callback: CallbackQuery, state: FSMContext):
    if not await require_admin_callback(callback):
        return
    sms_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(AdminSmsApproveState.invoice_token)
    await state.update_data(sms_id=sms_id)
    await callback.message.answer("شناسه پرداخت فاکتور مقصد را ارسال کنید:")
    await callback.answer()


@router.message(AdminSmsApproveState.invoice_token)
async def admin_sms_approve_finish(message: Message, state: FSMContext):
    if not await require_admin_message(message):
        await state.clear()
        return
    token = (message.text or "").strip()
    data = await state.get_data()
    async with SessionLocal() as session:
        sms = await session.get(SmsTransaction, data["sms_id"])
        invoice = await session.scalar(
            select(Invoice).where(Invoice.token == token).options(joinedload(Invoice.card), joinedload(Invoice.merchant))
        )
        if not sms or not invoice:
            return await message.answer("پیامک یا فاکتور پیدا نشد؛ شناسه پرداخت را دوباره بررسی کنید.")
        if sms.matched_invoice_id or sms.status == "matched":
            await state.clear()
            return await message.answer("این پیامک قبلاً استفاده شده است.")
        if invoice.status != "pending":
            await state.clear()
            return await message.answer(f"فاکتور در وضعیت {invoice.status} است.")
        if sms.amount_rial != invoice.payable_amount_rial:
            return await message.answer("مبلغ پیامک با مبلغ نهایی فاکتور برابر نیست.")
        if sms.card_last4 and sms.card_last4 != invoice.card.card_last4:
            return await message.answer("چهار رقم کارت پیامک با کارت مقصد فاکتور یکسان نیست.")
        if sms.bank_code not in {"generic", "unknown", invoice.card.bank_code}:
            return await message.answer("بانک پیامک با بانک کارت مقصد یکسان نیست.")
        paid_invoice = await confirm_invoice_paid(session, invoice.id, sms.id, sms.reference_number)
        if not paid_invoice:
            await state.clear()
            return await message.answer("تأیید انجام نشد؛ وضعیت فاکتور هم‌زمان تغییر کرده است.")
        sms.status = "matched"
        sms.matched_invoice_id = paid_invoice.id
        await session.commit()
    await state.clear()
    await send_paid_callback(paid_invoice)
    await message.answer("✅ پیامک با فاکتور تطبیق داده شد و پرداخت تأیید شد.", reply_markup=admin_menu())


@router.callback_query(F.data == "admin:cards")
async def admin_cards(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    async with SessionLocal() as session:
        result = list(
            (
                await session.execute(
                    select(BankCard, Merchant)
                    .join(Merchant, Merchant.id == BankCard.merchant_id)
                    .order_by(BankCard.id.desc())
                    .limit(15)
                )
            ).all()
        )
    text = "🏦 <b>آخرین کارت‌های ثبت‌شده</b>\n\n" + (
        "\n".join(
            f"#{card.id} • {'🟢' if card.is_active else '🔴'} {html.escape(card.bank_code)} ****{card.card_last4} • "
            f"{html.escape(short(merchant.name, 15))} ({merchant.telegram_user_id})"
            for card, merchant in result
        )
        if result
        else "کارتی ثبت نشده است."
    )
    await callback.message.edit_text(text, reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:ledger")
async def admin_ledger(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    async with SessionLocal() as session:
        result = list(
            (
                await session.execute(
                    select(WalletLedger, Merchant)
                    .join(Merchant, Merchant.id == WalletLedger.merchant_id)
                    .order_by(WalletLedger.id.desc())
                    .limit(15)
                )
            ).all()
        )
    lines = []
    for entry, merchant in result:
        sign = "+" if entry.amount_rial > 0 else ""
        lines.append(
            f"#{entry.id} • {html.escape(entry.entry_type)} • <b>{sign}{toman(entry.amount_rial)} ت</b> • "
            f"{merchant.telegram_user_id} • مانده {toman(entry.balance_after_rial)} ت"
        )
    await callback.message.edit_text(
        "💳 <b>آخرین گردش‌های کیف پول</b>\n\n" + ("\n".join(lines) if lines else "گردشی ثبت نشده است."),
        reply_markup=admin_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:system")
async def admin_system(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    backup = storage.status()
    async with SessionLocal() as session:
        last_update = await session.scalar(select(UpdateLog).order_by(UpdateLog.id.desc()).limit(1))
    has_error = bool(backup.get("last_error"))
    text = panel(
        "🖥",
        "سلامت و زیرساخت سامانه",
        [
            f"📦 نسخه فعال: <code>{APP_VERSION}</code>",
            f"🌐 دامنه عمومی: <code>{esc(settings.base_url)}</code>",
            f"🗂 مخزن: <code>{esc(settings.github_repository)}</code>",
            f"🌿 شاخه انتشار: <code>{esc(settings.github_branch)}</code>",
            f"🛡 شاخه پشتیبان داده: <code>{esc(settings.data_branch)}</code>",
            "",
            "<b>وضعیت ذخیره‌سازی</b>",
            f"📡 سلامت: <b>{badge('failed' if has_error else 'active')}</b>",
            f"🕓 پشتیبان در صف: <b>{'بله' if backup.get('dirty') else 'خیر'}</b>",
            f"💾 آخرین Backup: <code>{esc(backup.get('last_backup_at') or '-')}</code>",
            f"♻️ آخرین Restore: <code>{esc(backup.get('last_restore_at') or '-')}</code>",
            f"⚠️ آخرین خطا: <code>{esc(str(backup.get('last_error') or 'بدون خطا'))[:500]}</code>",
            "",
            f"🚀 آخرین نسخه منتشرشده: <code>{esc(last_update.version if last_update else '-')}</code>",
        ],
        subtitle="نسخه، استقرار، پشتیبان‌گیری و دسترس‌پذیری",
        footer="در صورت مشاهده خطا، پیش از انتشار نسخه جدید یک پشتیبان فوری تهیه کنید.",
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💾 تهیه پشتیبان فوری", callback_data="admin:backup")],
            [InlineKeyboardButton(text="📦 انتشار نسخه جدید", callback_data="admin:update")],
            [InlineKeyboardButton(text="‹ بازگشت به مرکز مدیریت", callback_data="admin:panel")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@router.callback_query(F.data == "admin:backup")
async def admin_backup(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer("در حال تهیه نسخه پشتیبان...", show_alert=False)
    ok = await storage.backup_now()
    await callback.message.answer(
        "✅ نسخه پشتیبان رمزنگاری‌شده در GitHub ذخیره شد."
        if ok
        else f"❌ پشتیبان‌گیری ناموفق بود:\n<code>{html.escape(storage.last_error or 'خطای نامشخص')}</code>"
    )


@router.callback_query(F.data == "admin:update")
async def admin_update(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.message.answer(
        panel(
            "📦",
            "انتشار نسخه جدید",
            [
                "فایل ZIP نسخه را در همین گفتگو ارسال کنید.",
                "بسته پیش از انتشار از نظر ساختار، فایل‌های ضروری و شماره نسخه اعتبارسنجی می‌شود.",
                "پس از ثبت Commit در GitHub، Railway استقرار خودکار را آغاز خواهد کرد.",
            ],
            subtitle="به‌روزرسانی امن از داخل ربات",
            footer="فایل‌های محرمانه، توکن‌ها و پایگاه داده را داخل ZIP قرار ندهید.",
        )
    )
    await callback.answer()

