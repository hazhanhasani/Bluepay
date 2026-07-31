from __future__ import annotations

import html
import io
import secrets
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.keyboards import (
    BANK_LABELS,
    api_menu,
    callback_menu,
    connection_menu,
    bank_select_menu,
    cards_menu,
    fee_menu,
    flow_cancel_menu,
    invoice_cards_menu,
    invoice_confirm_menu,
    invoice_fee_mode_menu,
    main_menu,
    payment_created_menu,
    sms_webhook_menu,
)
from app.bot.states import AddCardState, CallbackConfigState, ManualInvoiceState
from app.core.config import settings
from app.core.security import encrypt_text, random_secret
from app.core.urls import validate_public_https_url
from app.db.session import SessionLocal
from app.models import BankCard, Invoice, Merchant, SmsTransaction, UpdateLog
from app.services.callback_service import send_paid_callback, send_test_callback
from app.services.github_service import GitHubPublisher, validate_release_zip
from app.services.integration_service import merchant_docs_url, merchant_sms_webhook_url
from app.services.invoice_service import (
    calculate_customer_fee,
    confirm_invoice_paid,
    create_invoice,
    release_invoice_reservation,
)
from app.services.merchant_service import credit_wallet, get_or_create_merchant, regenerate_api_key
from app.services.settings_service import get_setting
from app.parsers import BANK_PROFILES, normalize_bank_code

router = Router()


_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def toman(value_rial: int) -> str:
    return f"{value_rial // 10:,}"


def digits_only(value: str | None) -> str:
    normalized = (value or "").translate(_DIGIT_TRANSLATION)
    return "".join(ch for ch in normalized if ch.isdigit())


def bank_title(code: str) -> str:
    normalized = normalize_bank_code(code)
    return BANK_LABELS.get(normalized, normalized.replace("_", " ").title())


def fee_title(mode: str) -> str:
    return {
        "customer": "👤 کامل با مشتری",
        "split": "🤝 نصف مشتری، نصف پذیرنده",
        "merchant": "🏪 کامل با پذیرنده",
    }.get(mode, mode)


def validate_callback_url(value: str) -> tuple[bool, str]:
    return validate_public_https_url(value)

async def current_merchant(user_id: int) -> Merchant | None:
    async with SessionLocal() as session:
        return await session.scalar(select(Merchant).where(Merchant.telegram_user_id == user_id))


async def home_view(user_id: int) -> tuple[str, Merchant | None]:
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == user_id))
        if not merchant:
            return "برای ساخت حساب ابتدا /start را بزن.", None

        card_count = int(
            await session.scalar(
                select(func.count(BankCard.id)).where(
                    BankCard.merchant_id == merchant.id,
                    BankCard.is_active.is_(True),
                )
            )
            or 0
        )
        pending_count = int(
            await session.scalar(
                select(func.count(Invoice.id)).where(
                    Invoice.merchant_id == merchant.id,
                    Invoice.status == "pending",
                )
            )
            or 0
        )

    status = "🟢 فعال" if merchant.is_active else "🔴 غیرفعال"
    text = f"""💠 <b>پنل درگاه BluePay</b>
━━━━━━━━━━━━━━━━
👤 <b>{html.escape(merchant.name)}</b>
📡 وضعیت حساب: <b>{status}</b>

💰 موجودی قابل استفاده: <b>{toman(merchant.available_balance_rial)} تومان</b>
🏦 کارت‌های فعال: <b>{card_count}</b>
🧾 فاکتورهای در انتظار: <b>{pending_count}</b>
⚙️ مدل کارمزد: <b>{fee_title(merchant.fee_mode)}</b>
━━━━━━━━━━━━━━━━
یکی از سرویس‌های زیر را انتخاب کن 👇"""
    return text, merchant


@router.message(CommandStart())
async def start(message: Message):
    async with SessionLocal() as session:
        merchant, created = await get_or_create_merchant(
            session,
            message.from_user.id,
            message.from_user.full_name,
        )
        await session.commit()

    text, merchant = await home_view(message.from_user.id)
    if created and merchant and merchant.is_admin:
        text += "\n\n👑 <b>مدیریت اصلی سیستم به حساب شما اختصاص یافت.</b>"
    await message.answer(text, reply_markup=main_menu(bool(merchant and merchant.is_admin)))


@router.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    text, merchant = await home_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=main_menu(bool(merchant and merchant.is_admin)))
    await callback.answer()


@router.callback_query(F.data == "wallet")
async def wallet(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا /start را بزن", show_alert=True)

    text = f"""💰 <b>کیف پول کارمزد</b>
━━━━━━━━━━━━━━━━
💵 موجودی کل
<b>{toman(merchant.wallet_balance_rial)} تومان</b>

🔒 رزروشده برای فاکتورها
<b>{toman(merchant.reserved_balance_rial)} تومان</b>

✅ قابل استفاده
<b>{toman(merchant.available_balance_rial)} تومان</b>

⚙️ هزینه هر تأیید پیامک
<b>{toman(merchant.verification_fee_rial)} تومان</b>
━━━━━━━━━━━━━━━━
کارمزد فقط پس از تأیید قطعی پرداخت از کیف پول کسر می‌شود."""
    await callback.message.edit_text(text, reply_markup=main_menu(merchant.is_admin))
    await callback.answer()


@router.callback_query(F.data == "fee")
async def fee(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا /start را بزن", show_alert=True)

    text = f"""⚙️ <b>تقسیم هزینه تأیید</b>
━━━━━━━━━━━━━━━━
مشخص کن هزینه درگاه هنگام ساخت فاکتور چگونه تقسیم شود:

👤 <b>مشتری:</b> تمام کارمزد به مبلغ فاکتور افزوده می‌شود.
🤝 <b>نصف‌نصف:</b> نصف کارمزد به فاکتور افزوده می‌شود.
🏪 <b>پذیرنده:</b> مبلغ فاکتور بدون افزایش می‌ماند.

انتخاب فعلی: <b>{fee_title(merchant.fee_mode)}</b>"""
    await callback.message.edit_text(text, reply_markup=fee_menu(merchant.fee_mode))
    await callback.answer()


@router.callback_query(F.data.startswith("fee:"))
async def set_fee(callback: CallbackQuery):
    mode = callback.data.split(":", 1)[1]
    if mode not in {"customer", "split", "merchant"}:
        return
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پیدا نشد", show_alert=True)
        merchant.fee_mode = mode
        await session.commit()

    await callback.message.edit_text(
        f"""✅ <b>تنظیم کارمزد ذخیره شد</b>
━━━━━━━━━━━━━━━━
مدل جدید: <b>{fee_title(mode)}</b>

این تنظیم به‌صورت پیش‌فرض روی فاکتورهای بعدی اعمال می‌شود.""",
        reply_markup=main_menu(merchant.is_admin),
    )
    await callback.answer("تنظیم شد ✅")


@router.callback_query(F.data == "cards")
async def cards(callback: CallbackQuery):
    text = """🏦 <b>مدیریت کارت‌های بانکی</b>
━━━━━━━━━━━━━━━━
برای هر فاکتور یک کارت مقصد انتخاب می‌شود. می‌توانی چند کارت از بانک‌های مختلف ثبت کنی."""
    await callback.message.edit_text(text, reply_markup=cards_menu())
    await callback.answer()


@router.callback_query(F.data == "card:add")
async def add_card_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddCardState.bank)
    await callback.message.edit_text(
        """🏦 <b>افزودن کارت — مرحله ۱ از ۴</b>
━━━━━━━━━━━━━━━━
بانک صادرکننده کارت را انتخاب کن:""",
        reply_markup=bank_select_menu(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("card:bankpage:"))
async def add_card_bank_page(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != AddCardState.bank.state:
        return await callback.answer("فرایند ثبت کارت فعال نیست.", show_alert=True)
    try:
        page = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        page = 0
    await callback.message.edit_reply_markup(reply_markup=bank_select_menu(page))
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("card:bank:"))
async def add_card_bank_button(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != AddCardState.bank.state:
        return await callback.answer("فرایند ثبت کارت فعال نیست.", show_alert=True)

    code = callback.data.rsplit(":", 1)[1]
    if code == "other":
        await callback.message.edit_text(
            """🏦 <b>افزودن کارت — مرحله ۱ از ۴</b>
━━━━━━━━━━━━━━━━
نام بانک را تایپ کن؛ مثال: آینده یا شهر""",
            reply_markup=flow_cancel_menu(),
        )
        return await callback.answer()

    await state.update_data(bank=code)
    await state.set_state(AddCardState.card_number)
    await callback.message.edit_text(
        f"""💳 <b>افزودن کارت — مرحله ۲ از ۴</b>
━━━━━━━━━━━━━━━━
بانک انتخاب‌شده: <b>{bank_title(code)}</b>

شماره ۱۶ رقمی کارت را ارسال کن:""",
        reply_markup=flow_cancel_menu(),
    )
    await callback.answer()


@router.message(AddCardState.bank)
async def add_card_bank(message: Message, state: FSMContext):
    code = normalize_bank_code(message.text)
    await state.update_data(bank=code)
    await state.set_state(AddCardState.card_number)
    await message.answer(
        f"""💳 <b>افزودن کارت — مرحله ۲ از ۴</b>
━━━━━━━━━━━━━━━━
بانک انتخاب‌شده: <b>{bank_title(code)}</b>

شماره ۱۶ رقمی کارت را ارسال کن:""",
        reply_markup=flow_cancel_menu(),
    )


@router.message(AddCardState.card_number)
async def add_card_number(message: Message, state: FSMContext):
    number = digits_only(message.text)
    if len(number) != 16:
        return await message.answer(
            "⚠️ شماره کارت باید دقیقاً ۱۶ رقم باشد. دوباره ارسال کن:",
            reply_markup=flow_cancel_menu(),
        )

    await state.update_data(card_number=number)
    await state.set_state(AddCardState.holder)
    await message.answer(
        f"""👤 <b>افزودن کارت — مرحله ۳ از ۴</b>
━━━━━━━━━━━━━━━━
شماره کارت: <code>**** **** **** {number[-4:]}</code>

نام صاحب کارت را ارسال کن:""",
        reply_markup=flow_cancel_menu(),
    )


@router.message(AddCardState.holder)
async def add_card_holder(message: Message, state: FSMContext):
    await state.update_data(holder=message.text.strip())
    await state.set_state(AddCardState.source_id)
    await message.answer(
        """📱 <b>افزودن کارت — مرحله ۴ از ۴</b>
━━━━━━━━━━━━━━━━
شناسه گوشی یا منبع پیامک را بفرست.
اگر فعلاً شناسه‌ای نداری، فقط <code>-</code> ارسال کن:""",
        reply_markup=flow_cancel_menu(),
    )


@router.message(AddCardState.source_id)
async def add_card_source(message: Message, state: FSMContext):
    data = await state.get_data()
    source_id = None if message.text.strip() == "-" else message.text.strip()
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        encryption_key = await get_setting(session, "encryption_key")
        if not merchant or not encryption_key:
            await state.clear()
            return await message.answer("خطای تنظیمات سیستم؛ دوباره تلاش کن.")

        existing_count = len(
            (
                await session.scalars(select(BankCard).where(BankCard.merchant_id == merchant.id))
            ).all()
        )
        card = BankCard(
            merchant_id=merchant.id,
            bank_code=data["bank"],
            card_number_encrypted=encrypt_text(data["card_number"], encryption_key),
            card_last4=data["card_number"][-4:],
            account_holder=data["holder"][:120],
            sms_source_id=source_id,
            is_default=(existing_count == 0),
        )
        session.add(card)
        await session.commit()

    await state.clear()
    await message.answer(
        f"""✅ <b>کارت بانکی ثبت شد</b>
━━━━━━━━━━━━━━━━
🏦 بانک: <b>{bank_title(data['bank'])}</b>
💳 شماره کارت: <code>**** **** **** {data['card_number'][-4:]}</code>
👤 صاحب کارت: <b>{html.escape(data['holder'])}</b>
📱 منبع پیامک: <b>{html.escape(source_id or 'ثبت نشده')}</b>""",
        reply_markup=main_menu(merchant.is_admin),
    )


@router.callback_query(F.data == "card:list")
async def list_cards(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        rows = (
            await session.scalars(
                select(BankCard)
                .where(BankCard.merchant_id == merchant.id)
                .order_by(BankCard.is_default.desc(), BankCard.id.asc())
            )
        ) if merchant else None
        card_rows = list(rows.all()) if rows else []

    if not card_rows:
        text = """🏦 <b>کارت‌های بانکی</b>
━━━━━━━━━━━━━━━━
هنوز هیچ کارتی ثبت نشده است. با دکمه «افزودن کارت جدید» اولین کارت را ثبت کن."""
    else:
        blocks: list[str] = []
        for card in card_rows:
            badges = []
            if card.is_default:
                badges.append("⭐ پیش‌فرض")
            badges.append("🟢 فعال" if card.is_active else "🔴 غیرفعال")
            blocks.append(
                f"""<b>💳 کارت #{card.id}</b>  {' • '.join(badges)}
🏦 {html.escape(bank_title(card.bank_code))}
🔢 <code>**** **** **** {card.card_last4}</code>
👤 {html.escape(card.account_holder)}"""
            )
        text = "🏦 <b>کارت‌های بانکی من</b>\n━━━━━━━━━━━━━━━━\n\n" + "\n\n────────────\n\n".join(blocks)

    await callback.message.edit_text(text, reply_markup=cards_menu())
    await callback.answer()


@router.callback_query(F.data == "flow:cancel")
async def cancel_flow(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text, merchant = await home_view(callback.from_user.id)
    await callback.message.edit_text(
        "❌ <b>عملیات لغو شد</b>\n\n" + text,
        reply_markup=main_menu(bool(merchant and merchant.is_admin)),
    )
    await callback.answer("عملیات لغو شد")


@router.callback_query(F.data.in_({"invoice:new", "invoice:restart"}))
async def invoice_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ManualInvoiceState.amount)
    await callback.message.edit_text(
        """🧾 <b>ساخت فاکتور — مرحله ۱ از ۴</b>
━━━━━━━━━━━━━━━━
💵 مبلغ سفارش را به <b>تومان</b> ارسال کن.

مثال: <code>200000</code>""",
        reply_markup=flow_cancel_menu(),
    )
    await callback.answer()


@router.message(ManualInvoiceState.amount)
async def invoice_amount(message: Message, state: FSMContext):
    raw = digits_only(message.text)
    if not raw or int(raw) < 1000:
        return await message.answer(
            "⚠️ مبلغ باید عددی و حداقل ۱٬۰۰۰ تومان باشد. دوباره ارسال کن:",
            reply_markup=flow_cancel_menu(),
        )

    amount = int(raw)
    await state.update_data(amount_toman=amount)
    await state.set_state(ManualInvoiceState.description)
    await message.answer(
        f"""📝 <b>ساخت فاکتور — مرحله ۲ از ۴</b>
━━━━━━━━━━━━━━━━
مبلغ سفارش: <b>{amount:,} تومان</b>

عنوان یا توضیح کوتاه فاکتور را ارسال کن:
مثال: <code>خرید اشتراک یک‌ماهه</code>""",
        reply_markup=flow_cancel_menu(),
    )


@router.message(ManualInvoiceState.description)
async def invoice_description(message: Message, state: FSMContext):
    description = message.text.strip()[:500]
    if not description:
        return await message.answer("عنوان فاکتور نمی‌تواند خالی باشد.", reply_markup=flow_cancel_menu())

    await state.update_data(description=description)
    await state.set_state(ManualInvoiceState.fee_mode)
    merchant = await current_merchant(message.from_user.id)
    if not merchant:
        await state.clear()
        return await message.answer("حساب پذیرنده پیدا نشد؛ /start را بزن.")

    await message.answer(
        """⚙️ <b>ساخت فاکتور — مرحله ۳ از ۴</b>
━━━━━━━━━━━━━━━━
نحوه پرداخت کارمزد این فاکتور را انتخاب کن:""",
        reply_markup=invoice_fee_mode_menu(merchant.fee_mode),
    )


@router.message(ManualInvoiceState.fee_mode)
async def invoice_fee_mode_text(message: Message, state: FSMContext):
    mode = message.text.strip().lower()
    merchant = await current_merchant(message.from_user.id)
    if not merchant:
        await state.clear()
        return await message.answer("حساب پذیرنده پیدا نشد.")
    if mode not in {"customer", "split", "merchant", "default"}:
        return await message.answer(
            "از دکمه‌های زیر انتخاب کن:",
            reply_markup=invoice_fee_mode_menu(merchant.fee_mode),
        )
    await state.update_data(fee_mode=None if mode == "default" else mode)
    await show_invoice_cards(message, state, message.from_user.id)


async def show_invoice_cards(target: Message, state: FSMContext, user_id: int) -> None:
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == user_id))
        cards = list(
            (
                await session.scalars(
                    select(BankCard)
                    .where(BankCard.merchant_id == merchant.id, BankCard.is_active.is_(True))
                    .order_by(BankCard.is_default.desc(), BankCard.priority.asc(), BankCard.id.asc())
                )
            ).all()
        ) if merchant else []

    if not cards:
        await state.clear()
        await target.answer(
            "⚠️ <b>کارت فعالی پیدا نشد</b>\n\nابتدا از بخش «کارت‌های من» یک کارت بانکی ثبت کن.",
            reply_markup=cards_menu(),
        )
        return

    await state.set_state(ManualInvoiceState.card)
    keyboard_data = [(c.id, bank_title(c.bank_code), c.card_last4, c.is_default) for c in cards]
    await target.answer(
        """🏦 <b>ساخت فاکتور — مرحله ۴ از ۴</b>
━━━━━━━━━━━━━━━━
کارت مقصد را انتخاب کن. «انتخاب هوشمند» کارت پیش‌فرض یا اولویت‌دار را برمی‌گزیند:""",
        reply_markup=invoice_cards_menu(keyboard_data),
    )


@router.callback_query(F.data.startswith("invoice:fee:"))
async def invoice_fee_mode_button(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != ManualInvoiceState.fee_mode.state:
        return await callback.answer("فرایند ساخت فاکتور منقضی شده؛ دوباره شروع کن.", show_alert=True)

    mode = callback.data.rsplit(":", 1)[1]
    await state.update_data(fee_mode=None if mode == "default" else mode)

    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        cards = list(
            (
                await session.scalars(
                    select(BankCard)
                    .where(BankCard.merchant_id == merchant.id, BankCard.is_active.is_(True))
                    .order_by(BankCard.is_default.desc(), BankCard.priority.asc(), BankCard.id.asc())
                )
            ).all()
        ) if merchant else []

    if not cards:
        await state.clear()
        await callback.message.edit_text(
            "⚠️ <b>کارت فعالی پیدا نشد</b>\n\nابتدا یک کارت بانکی ثبت کن.",
            reply_markup=cards_menu(),
        )
        return await callback.answer()

    await state.set_state(ManualInvoiceState.card)
    keyboard_data = [(c.id, bank_title(c.bank_code), c.card_last4, c.is_default) for c in cards]
    await callback.message.edit_text(
        """🏦 <b>ساخت فاکتور — مرحله ۴ از ۴</b>
━━━━━━━━━━━━━━━━
کارت مقصد را انتخاب کن:""",
        reply_markup=invoice_cards_menu(keyboard_data),
    )
    await callback.answer()


@router.message(ManualInvoiceState.card)
async def invoice_card_text(message: Message, state: FSMContext):
    raw = digits_only(message.text)
    if not raw:
        return await message.answer("کارت را از دکمه‌ها انتخاب کن یا شناسه عددی کارت را بفرست.")
    await prepare_invoice_preview(message, state, message.from_user.id, int(raw))


@router.callback_query(F.data.startswith("invoice:card:"))
async def invoice_card_button(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != ManualInvoiceState.card.state:
        return await callback.answer("فرایند ساخت فاکتور منقضی شده؛ دوباره شروع کن.", show_alert=True)

    card_id = int(callback.data.rsplit(":", 1)[1])
    await prepare_invoice_preview(callback.message, state, callback.from_user.id, card_id, edit=True)
    await callback.answer()


async def prepare_invoice_preview(
    target: Message,
    state: FSMContext,
    user_id: int,
    requested_card_id: int,
    *,
    edit: bool = False,
) -> None:
    data = await state.get_data()
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == user_id))
        if not merchant:
            await state.clear()
            return await target.answer("حساب پذیرنده پیدا نشد.")

        stmt = select(BankCard).where(BankCard.merchant_id == merchant.id, BankCard.is_active.is_(True))
        if requested_card_id:
            stmt = stmt.where(BankCard.id == requested_card_id)
        else:
            stmt = stmt.order_by(BankCard.is_default.desc(), BankCard.priority.asc(), BankCard.id.asc())
        card = await session.scalar(stmt.limit(1))

    if not card:
        return await target.answer("کارت انتخاب‌شده معتبر یا فعال نیست.", reply_markup=flow_cancel_menu())

    mode = data.get("fee_mode") or merchant.fee_mode
    fee_rial = merchant.verification_fee_rial
    customer_fee_rial = calculate_customer_fee(fee_rial, mode)
    base_rial = data["amount_toman"] * 10
    payable_rial = base_rial + customer_fee_rial

    await state.update_data(resolved_fee_mode=mode, selected_card_id=card.id)
    await state.set_state(ManualInvoiceState.confirm)

    preview = f"""🧾 <b>پیش‌نمایش نهایی فاکتور</b>
━━━━━━━━━━━━━━━━
📝 عنوان: <b>{html.escape(data['description'])}</b>

💵 مبلغ سفارش: <b>{toman(base_rial)} تومان</b>
⚙️ کارمزد تأیید: <b>{toman(fee_rial)} تومان</b>
👤 سهم مشتری از کارمزد: <b>{toman(customer_fee_rial)} تومان</b>
💳 مبلغ نهایی پرداخت: <b>{toman(payable_rial)} تومان</b>

🏦 کارت مقصد: <b>{bank_title(card.bank_code)} •••• {card.card_last4}</b>
🤝 مدل کارمزد: <b>{fee_title(mode)}</b>
⏳ اعتبار لینک: <b>{settings.invoice_ttl_minutes} دقیقه</b>
━━━━━━━━━━━━━━━━
در صورت تأیید، کارمزد از موجودی قابل استفاده رزرو می‌شود."""

    if edit:
        await target.edit_text(preview, reply_markup=invoice_confirm_menu())
    else:
        await target.answer(preview, reply_markup=invoice_confirm_menu())


@router.callback_query(F.data == "invoice:confirm")
async def invoice_confirm(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != ManualInvoiceState.confirm.state:
        return await callback.answer("اطلاعات فاکتور منقضی شده؛ دوباره شروع کن.", show_alert=True)

    data = await state.get_data()
    await state.set_state(ManualInvoiceState.processing)
    try:
        async with SessionLocal() as session:
            merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
            invoice = await create_invoice(
                session,
                merchant,
                base_amount_rial=data["amount_toman"] * 10,
                description=data["description"],
                fee_mode=data["resolved_fee_mode"],
                card_id=data["selected_card_id"],
            )
            card = await session.get(BankCard, invoice.card_id)
            await session.commit()

        payment_url = f"{settings.base_url}/pay/{invoice.token}"
        await state.clear()
        await callback.message.edit_text(
            f"""✅ <b>فاکتور با موفقیت ساخته شد</b>
━━━━━━━━━━━━━━━━
🧾 شناسه: <code>{invoice.token}</code>
📝 عنوان: <b>{html.escape(invoice.description or '-')}</b>
💳 مبلغ پرداخت: <b>{toman(invoice.payable_amount_rial)} تومان</b>
🏦 مقصد: <b>{bank_title(card.bank_code)} •••• {card.card_last4}</b>
⏳ وضعیت: <b>در انتظار پرداخت</b>

🔗 لینک پرداخت:
{payment_url}""",
            reply_markup=payment_created_menu(payment_url),
        )
        await callback.answer("فاکتور ساخته شد ✅")
    except Exception as exc:
        await state.set_state(ManualInvoiceState.confirm)
        await callback.answer("ساخت فاکتور ناموفق بود", show_alert=True)
        await callback.message.edit_text(
            f"""❌ <b>ساخت فاکتور ناموفق بود</b>
━━━━━━━━━━━━━━━━
<code>{html.escape(str(exc))}</code>""",
            reply_markup=invoice_confirm_menu(),
        )
@router.callback_query(F.data == "connect")
async def connection_panel(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا /start را بزن", show_alert=True)
    docs_url = merchant_docs_url(merchant)
    callback_status = "🟢 متصل" if merchant.callback_url else "🟡 تنظیم نشده"
    api_status = "🟢 ساخته شده" if merchant.api_key_prefix else "🟡 ساخته نشده"
    await callback.message.edit_text(
        f"""🔌 <b>مرکز اتصال BluePay</b>
━━━━━━━━━━━━━━━━
از این بخش سایت، ربات و SMS Forwarder را به درگاه وصل کن.

<b>وضعیت اتصال‌ها</b>
🔑 API پذیرنده: <b>{api_status}</b>
📲 وبهوک پیامک اختصاصی: <b>🟢 آماده</b>
🔔 Callback نتیجه پرداخت: <b>{callback_status}</b>

<b>ترتیب پیشنهادی راه‌اندازی</b>
1️⃣ کلید API بساز.
2️⃣ وبهوک اختصاصی پیامک را در گوشی ثبت کن.
3️⃣ آدرس Callback سایت یا رباتت را وارد کن.
4️⃣ از صفحه مستندات نمونه کد را بردار و تست کن.
━━━━━━━━━━━━━━━━
📘 مستندات اختصاصی حساب:
<code>{html.escape(docs_url)}</code>""",
        reply_markup=connection_menu(docs_url),
    )
    await callback.answer()


@router.callback_query(F.data == "api")
async def api_panel(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا /start را بزن", show_alert=True)
    prefix = merchant.api_key_prefix or "ساخته نشده"
    callback_secret = merchant.callback_secret or "ساخته نشده"
    callback_url = merchant.callback_url or "تنظیم نشده"
    docs_url = merchant_docs_url(merchant)
    await callback.message.edit_text(
        f"""🔑 <b>API پذیرنده</b>
━━━━━━━━━━━━━━━━
🪪 پیشوند کلید فعلی
<code>{html.escape(prefix)}</code>

🌐 آدرس پایه API
<code>{settings.base_url}/api/v1</code>

🔐 Secret امضای Callback
<code>{html.escape(callback_secret)}</code>

🔔 آدرس Callback پیش‌فرض
<code>{html.escape(callback_url)}</code>
━━━━━━━━━━━━━━━━
کلید کامل API فقط یک‌بار هنگام ساخت نمایش داده می‌شود.
هدر تمام درخواست‌ها:
<code>X-API-Key: gw_...</code>""",
        reply_markup=api_menu(docs_url),
    )
    await callback.answer()


@router.callback_query(F.data == "api:regen")
async def api_regen(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پیدا نشد", show_alert=True)
        key = await regenerate_api_key(session, merchant)
        await session.commit()
    await callback.message.answer(
        "⚠️ <b>کلید API جدید ساخته شد</b>\n\n"
        "این کلید را همین حالا ذخیره کن؛ دوباره نمایش داده نمی‌شود:\n\n"
        f"<code>{key}</code>\n\n"
        "در درخواست‌های سایت یا ربات، هدر <code>X-API-Key</code> را برابر این مقدار قرار بده."
    )
    await callback.answer("کلید جدید ساخته شد")


@router.callback_query(F.data == "sms:webhook")
async def sms_info(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا /start را بزن", show_alert=True)
    webhook_url = merchant_sms_webhook_url(merchant)
    docs_url = merchant_docs_url(merchant)
    await callback.message.edit_text(
        f"""📲 <b>وبهوک اختصاصی پیامک</b>
━━━━━━━━━━━━━━━━
این آدرس فقط متعلق به حساب شماست و پیامک‌های ارسالی از آن فقط با کارت‌ها و فاکتورهای خودت تطبیق داده می‌شوند.

🌐 <b>Webhook URL</b>
<code>{html.escape(webhook_url)}</code>

📤 روش درخواست
<code>POST</code> با <code>Content-Type: application/json</code>

📦 بدنه نمونه
<code>{{"sender":"Bank Mellat","message":"متن کامل پیامک بانک","device_id":"phone-1","bank_code":"mellat"}}</code>

<b>راهنمای اتصال در SMS Forwarder</b>
1️⃣ نوع درخواست را POST انتخاب کن.
2️⃣ آدرس بالا را بدون تغییر وارد کن.
3️⃣ بدنه را روی JSON بگذار.
4️⃣ sender، message و device_id را ارسال کن. بهتر است bank_code را هم مطابق مستندات بفرستی.
5️⃣ یک پیامک آزمایشی بفرست و پاسخ success را بررسی کن.

🏦 پوشش فعلی: <b>{len(BANK_PROFILES)} بانک، مؤسسه و برند بانکی</b>
━━━━━━━━━━━━━━━━
⚠️ این URL محرمانه است؛ آن را در اختیار شخص دیگری قرار نده.""",
        reply_markup=sms_webhook_menu(docs_url),
    )
    await callback.answer()


@router.callback_query(F.data == "callback:panel")
async def callback_panel(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا /start را بزن", show_alert=True)
    docs_url = merchant_docs_url(merchant)
    status = "🟢 فعال" if merchant.callback_url else "🟡 تنظیم نشده"
    await callback.message.edit_text(
        f"""🔔 <b>وبهوک نتیجه پرداخت</b>
━━━━━━━━━━━━━━━━
پس از تأیید پرداخت، BluePay نتیجه را به آدرس سایت یا ربات شما ارسال می‌کند.

📡 وضعیت: <b>{status}</b>
🌐 آدرس فعلی:
<code>{html.escape(merchant.callback_url or 'تنظیم نشده')}</code>

🔐 Secret امضای اختصاصی:
<code>{html.escape(merchant.callback_secret or 'ساخته نشده')}</code>

هدرهای ارسالی:
<code>X-Gateway-Signature</code>
<code>X-Gateway-Event</code>
<code>X-Gateway-Delivery</code>
━━━━━━━━━━━━━━━━
سایت شما باید پاسخ HTTP بین 200 تا 299 برگرداند.""",
        reply_markup=callback_menu(docs_url, bool(merchant.callback_url)),
    )
    await callback.answer()


@router.callback_query(F.data == "callback:set")
async def callback_set_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CallbackConfigState.url)
    await callback.message.edit_text(
        """✏️ <b>ثبت آدرس Callback</b>
━━━━━━━━━━━━━━━━
آدرس HTTPS دریافت نتیجه پرداخت را ارسال کن.

مثال:
<code>https://example.com/api/bluepay/webhook</code>

این آدرس باید از اینترنت در دسترس باشد و در کمتر از ۱۲ ثانیه پاسخ دهد.""",
        reply_markup=flow_cancel_menu(),
    )
    await callback.answer()


@router.message(CallbackConfigState.url)
async def callback_set_value(message: Message, state: FSMContext):
    valid, result = validate_callback_url(message.text or "")
    if not valid:
        return await message.answer(f"⚠️ {html.escape(result)}\nدوباره آدرس را ارسال کن:", reply_markup=flow_cancel_menu())
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not merchant:
            await state.clear()
            return await message.answer("حساب پیدا نشد؛ /start را بزن.")
        merchant.callback_url = result
        if not merchant.callback_secret:
            merchant.callback_secret = random_secret(32)
        await session.commit()
    await state.clear()
    await message.answer(
        f"""✅ <b>Callback ذخیره شد</b>
━━━━━━━━━━━━━━━━
🌐 آدرس:
<code>{html.escape(result)}</code>

اکنون از بخش «آموزش و اتصال» دکمه تست اتصال را بزن.""",
        reply_markup=main_menu(merchant.is_admin),
    )


@router.callback_query(F.data == "callback:remove")
async def callback_remove(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پیدا نشد", show_alert=True)
        merchant.callback_url = None
        await session.commit()
    await callback.answer("Callback حذف شد", show_alert=True)
    await callback.message.edit_text(
        "✅ آدرس Callback حذف شد. تا زمان ثبت آدرس جدید، نتیجه پرداخت فقط از طریق استعلام API قابل دریافت است.",
        reply_markup=connection_menu(merchant_docs_url(merchant)),
    )


@router.callback_query(F.data == "callback:secret")
async def callback_secret_regen(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پیدا نشد", show_alert=True)
        merchant.callback_secret = random_secret(32)
        await session.commit()
        secret = merchant.callback_secret
    await callback.message.answer(
        "🔐 <b>Secret جدید ساخته شد</b>\n\n"
        f"<code>{html.escape(secret)}</code>\n\n"
        "⚠️ با تغییر Secret، لینک وبهوک اختصاصی پیامک و لینک مستندات اختصاصی نیز تغییر می‌کنند؛ آدرس جدید را دوباره از بخش اتصال بردار."
    )
    await callback.answer("Secret تغییر کرد")


@router.callback_query(F.data == "callback:test")
async def callback_test(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant or not merchant.callback_url:
        return await callback.answer("ابتدا آدرس Callback را ثبت کن", show_alert=True)
    await callback.answer("در حال ارسال تست…")
    ok, result = await send_test_callback(merchant)
    if ok:
        await callback.message.answer(
            "✅ <b>تست Callback موفق بود</b>\n\n"
            f"آدرس مقصد با موفقیت پاسخ داد: <code>{html.escape(result)}</code>"
        )
    else:
        await callback.message.answer(
            "❌ <b>تست Callback ناموفق بود</b>\n\n"
            f"نتیجه: <code>{html.escape(result)}</code>\n"
            "لاگ سرور مقصد، SSL و پاسخ HTTP را بررسی کن."
        )


@router.message(Command("credit"))
async def admin_credit(message: Message):
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("فرمت: /credit TELEGRAM_ID AMOUNT_TOMAN")
    async with SessionLocal() as session:
        admin = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not admin or not admin.is_admin:
            return await message.answer("این دستور فقط برای مدیر است.")
        target = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == int(parts[1])))
        if not target:
            return await message.answer("کاربر مقصد یافت نشد.")
        amount_rial = int(parts[2]) * 10
        await credit_wallet(session, target, amount_rial, f"شارژ توسط مدیر {admin.telegram_user_id}", f"admin:{message.message_id}:{target.id}")
        await session.commit()
    await message.answer(f"✅ کیف پول کاربر {parts[1]} به مبلغ {int(parts[2]):,} تومان شارژ شد.")


@router.message(Command("callback"))
async def callback_config(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        return await message.answer("فرمت: /callback https://example.com/payment/callback یا /callback -")
    value = parts[1].strip()
    if value != "-":
        valid, result = validate_callback_url(value)
        if not valid:
            return await message.answer(f"⚠️ {result}")
        value = result
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not merchant:
            return await message.answer("ابتدا /start را بزن.")
        merchant.callback_url = None if value == "-" else value
        if not merchant.callback_secret:
            merchant.callback_secret = random_secret(32)
        await session.commit()
    await message.answer("✅ آدرس Callback ذخیره شد." if value != "-" else "✅ Callback پیش‌فرض حذف شد.")


@router.message(Command("cancel"))
async def cancel_invoice(message: Message):
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer("فرمت: /cancel PAYMENT_ID")
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not merchant:
            return await message.answer("ابتدا /start را بزن.")
        invoice = await session.scalar(
            select(Invoice).where(Invoice.token == parts[1], Invoice.merchant_id == merchant.id)
        )
        if not invoice:
            return await message.answer("فاکتور پیدا نشد.")
        if invoice.status != "pending":
            return await message.answer(f"این فاکتور قابل لغو نیست؛ وضعیت: {invoice.status}")
        await release_invoice_reservation(session, invoice, "cancelled")
        await session.commit()
    await message.answer("✅ فاکتور لغو و کارمزد رزروشده آزاد شد.")


@router.message(Command("setfee"))
async def admin_set_fee(message: Message):
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("فرمت: /setfee TELEGRAM_ID AMOUNT_TOMAN")
    try:
        target_id = int(parts[1])
        fee_rial = int(parts[2]) * 10
        if fee_rial <= 0:
            raise ValueError
    except ValueError:
        return await message.answer("شناسه یا مبلغ نامعتبر است.")
    async with SessionLocal() as session:
        admin = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not admin or not admin.is_admin:
            return await message.answer("این دستور فقط برای مدیر است.")
        target = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == target_id))
        if not target:
            return await message.answer("کاربر مقصد یافت نشد.")
        target.verification_fee_rial = fee_rial
        await session.commit()
    await message.answer(f"✅ کارمزد کاربر {target_id} روی {int(parts[2]):,} تومان تنظیم شد.")


@router.message(Command("reviews"))
async def admin_reviews(message: Message):
    async with SessionLocal() as session:
        admin = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not admin or not admin.is_admin:
            return await message.answer("این دستور فقط برای مدیر است.")
        rows = list((await session.scalars(
            select(SmsTransaction)
            .where(SmsTransaction.status.in_(["review", "unmatched"]))
            .order_by(SmsTransaction.id.desc())
            .limit(10)
        )).all())
    if not rows:
        return await message.answer("هیچ پیامک نیازمند بررسی وجود ندارد.")
    text = "🔎 <b>پیامک‌های نیازمند بررسی</b>\n\n" + "\n\n".join(
        f"SMS #{row.id} | {html.escape(row.bank_code)} | "
        f"{toman(row.amount_rial or 0)} تومان | کارت {row.card_last4 or '-'}\n"
        f"<code>{html.escape(row.raw_message[:220])}</code>"
        for row in rows
    )
    text += "\n\nتأیید دستی: <code>/approve SMS_ID PAYMENT_ID</code>"
    await message.answer(text)


@router.message(Command("approve"))
async def admin_approve_sms(message: Message):
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("فرمت: /approve SMS_ID PAYMENT_ID")
    try:
        sms_id = int(parts[1])
    except ValueError:
        return await message.answer("شناسه پیامک نامعتبر است.")
    async with SessionLocal() as session:
        admin = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not admin or not admin.is_admin:
            return await message.answer("این دستور فقط برای مدیر است.")
        sms = await session.get(SmsTransaction, sms_id)
        invoice = await session.scalar(select(Invoice).where(Invoice.token == parts[2]))
        if not sms or not invoice:
            return await message.answer("پیامک یا فاکتور پیدا نشد.")
        if sms.matched_invoice_id or sms.status == "matched":
            return await message.answer("این پیامک قبلاً استفاده شده است.")
        if invoice.status != "pending":
            return await message.answer(f"فاکتور در وضعیت {invoice.status} است.")
        if sms.amount_rial != invoice.payable_amount_rial:
            return await message.answer("مبلغ پیامک و مبلغ قابل پرداخت فاکتور برابر نیست.")
        paid_invoice = await confirm_invoice_paid(session, invoice.id, sms.id, sms.reference_number)
        if not paid_invoice:
            return await message.answer("تأیید انجام نشد؛ وضعیت هم‌زمان تغییر کرده است.")
        sms.status = "matched"
        sms.matched_invoice_id = paid_invoice.id
        await session.commit()
    await send_paid_callback(paid_invoice)
    await message.answer("✅ پیامک به‌صورت دستی به فاکتور متصل و پرداخت تأیید شد.")


@router.message(Command("rejectsms"))
async def admin_reject_sms(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("فرمت: /rejectsms SMS_ID")
    async with SessionLocal() as session:
        admin = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not admin or not admin.is_admin:
            return await message.answer("این دستور فقط برای مدیر است.")
        sms = await session.get(SmsTransaction, int(parts[1]))
        if not sms:
            return await message.answer("پیامک پیدا نشد.")
        if sms.status == "matched":
            return await message.answer("پیامک تأییدشده قابل ردکردن نیست.")
        sms.status = "rejected"
        await session.commit()
    await message.answer("✅ پیامک رد شد.")


@router.message(Command("github"))
async def github_status(message: Message):
    async with SessionLocal() as session:
        admin = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not admin or not admin.is_admin:
            return await message.answer("این دستور فقط برای مدیر است.")
    await message.answer(
        "✅ مخزن و شاخه به‌صورت خودکار از Railway تشخیص داده شده‌اند.\n\n"
        f"مخزن: <code>{html.escape(settings.github_repository)}</code>\n"
        f"شاخه انتشار: <code>{html.escape(settings.github_branch)}</code>\n"
        f"شاخه دیتابیس رمزنگاری‌شده: <code>{html.escape(settings.data_branch)}</code>"
    )


@router.callback_query(F.data == "update:help")
async def update_help(callback: CallbackQuery):
    await callback.message.answer(
        "📦 فایل ZIP نسخه جدید را همین‌جا ارسال کن.\n"
        "مخزن و شاخه به‌صورت خودکار از Deploy متصل Railway تشخیص داده می‌شوند؛ تنظیم دیگری لازم نیست."
    )
    await callback.answer()


@router.message(F.document)
async def release_upload(message: Message):
    async with SessionLocal() as session:
        admin = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not admin or not admin.is_admin:
            return
        repository = settings.github_repository
        branch = settings.github_branch
    if not message.document.file_name.lower().endswith(".zip"):
        return await message.answer("فقط فایل ZIP نسخه را ارسال کن.")

    status = await message.answer("📦 در حال بررسی بسته نسخه...")
    buffer = io.BytesIO()
    await message.bot.download(message.document, destination=buffer)
    try:
        package = validate_release_zip(buffer.getvalue())
        await status.edit_text(f"✅ بسته نسخه {html.escape(package.version)} معتبر است. در حال انتشار در GitHub...")
        publisher = GitHubPublisher(settings.github_token, repository, branch or "main")
        commit_sha = await publisher.publish(package)
        async with SessionLocal() as session:
            session.add(
                UpdateLog(
                    version=package.version,
                    commit_sha=commit_sha,
                    status="published",
                    message=package.description,
                    telegram_user_id=message.from_user.id,
                )
            )
            await session.commit()
        await status.edit_text(
            "🚀 نسخه در GitHub منتشر شد. Railway باید Deploy خودکار را آغاز کند.\n\n"
            f"نسخه: <code>{html.escape(package.version)}</code>\n"
            f"Commit: <code>{commit_sha[:12]}</code>"
        )
    except Exception as exc:
        await status.edit_text(f"❌ انتشار نسخه ناموفق بود:\n<code>{html.escape(str(exc))}</code>")
