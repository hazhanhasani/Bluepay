from __future__ import annotations

import html
import io
import json
import secrets
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.bot.access import show_access_prompt
from app.bot.keyboards import (
    BANK_LABELS,
    account_menu,
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
    invoices_menu,
    main_menu,
    payment_created_menu,
    sms_webhook_menu,
    sms_webhook_rotate_confirm_menu,
    store_detail_menu,
    stores_menu,
    wallet_menu,
    wallet_topup_created_menu,
    wallet_topup_menu,
)
from app.bot.states import (
    AddCardState,
    CallbackConfigState,
    ManualInvoiceState,
    StoreCallbackState,
    StoreCreateState,
    WalletTopUpState,
    AdminReleaseState,
)
from app.bot.presentation import (
    DIVIDER,
    badge,
    error,
    esc,
    fee_mode_label,
    field,
    info,
    invoice_status_label,
    money_toman,
    panel,
    progress,
    success,
    warning,
)
from app.core.config import settings
from app.core.security import encrypt_text, random_secret
from app.core.urls import validate_public_https_url
from app.db.session import SessionLocal
from app.models import BankCard, Invoice, Merchant, MerchantTeamMember, SmsDevice, SmsTransaction, Store, StoreApiKey, UpdateLog, WalletLedger
from app.services.access_service import evaluate_access
from app.services.callback_service import send_paid_callback, send_store_test_callback, send_test_callback
from app.services.github_service import GitHubPublisher, validate_release_zip
from app.services.integration_service import merchant_docs_url, merchant_sms_webhook_url, rotate_merchant_sms_token
from app.services.invoice_service import (
    calculate_customer_fee,
    confirm_invoice_paid,
    create_invoice,
    create_wallet_topup_invoice,
    release_invoice_reservation,
)
from app.services.merchant_service import credit_wallet, get_or_create_merchant
from app.services.portal_service import merchant_portal_url
from app.services.sms_device_service import register_sms_device, rotate_sms_device_secret, list_sms_devices
from app.services.team_service import add_team_member, list_team_members, remove_team_member, role_label, team_portal_url
from app.services.settings_service import get_setting
from app.services.store_service import (
    create_store,
    get_owned_store,
    issue_store_api_key,
    list_merchant_stores,
    rotate_store_api_key,
    set_key_active,
)
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
    return fee_mode_label(mode)


def validate_callback_url(value: str) -> tuple[bool, str]:
    return validate_public_https_url(value)

async def current_merchant(user_id: int) -> Merchant | None:
    async with SessionLocal() as session:
        return await session.scalar(select(Merchant).where(Merchant.telegram_user_id == user_id))


async def home_view(user_id: int) -> tuple[str, Merchant | None]:
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == user_id))
        if not merchant:
            return warning(
                "حساب پذیرنده فعال نیست",
                "برای ایجاد حساب و ورود به پنل، دستور <code>/start</code> را ارسال کنید.",
            ), None

    text = panel(
        "💠",
        "به بلوپی خوش آمدید",
        [
            "بلوپی، زیرساخت یکپارچه مدیریت پرداخت مستقیم برای کسب‌وکارهای آنلاین است.",
            "",
            "از این پنل می‌توانید:",
            "• فاکتور پرداخت صادر کنید.",
            "• کارت‌های دریافت وجه را مدیریت کنید.",
            "• پرداخت‌ها را با پیامک بانکی تأیید کنید.",
            "• سایت یا ربات خود را از طریق API و Callback متصل کنید.",
        ],
        subtitle="صدور، دریافت و تأیید هوشمند پرداخت",
        footer="برای شروع، یکی از گزینه‌های زیر را انتخاب کنید.",
    )
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
        decision = await evaluate_access(
            session,
            message.bot,
            merchant,
            force_membership_refresh=True,
        )

    if not decision.allowed:
        await show_access_prompt(message, decision)
        return

    text, merchant = await home_view(message.from_user.id)
    if created and merchant and merchant.is_admin:
        text += "\n\n👑 <b>این حساب به‌عنوان مدیر اصلی سامانه ثبت شد.</b>"
    await message.answer(text, reply_markup=main_menu(bool(merchant and merchant.is_admin)))


@router.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    text, merchant = await home_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=main_menu(bool(merchant and merchant.is_admin)))
    await callback.answer()


@router.callback_query(F.data == "account")
async def account_panel(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)

    text = panel(
        "👤",
        "حساب پذیرنده",
        [
            f"👤 نام حساب: <b>{esc(merchant.name)}</b>",
            f"🪪 شناسه پذیرنده: <code>BP-{merchant.id:06d}</code>",
            f"📡 وضعیت حساب: <b>{badge('active' if merchant.is_active else 'inactive')}</b>",
            f"🛡 سطح دسترسی: <b>{'مدیر سامانه' if merchant.is_admin else 'پذیرنده'}</b>",
            f"📱 احراز شماره: <b>{'تأییدشده ••••' + esc(merchant.phone_last4) if merchant.phone_verified_at and merchant.phone_last4 else 'تأیید نشده'}</b>",
            f"📅 تاریخ عضویت: <b>{merchant.created_at.strftime('%Y-%m-%d') if merchant.created_at else '-'}</b>",
        ],
        subtitle="مشخصات و وضعیت حساب کاربری",
        footer="اطلاعات مالی، کارت‌ها و اتصال‌های فنی از بخش‌های اختصاصی پنل در دسترس هستند.",
    )
    await callback.message.edit_text(text, reply_markup=account_menu())
    await callback.answer()


@router.callback_query(F.data == "invoices")
async def invoices_panel(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)

        pending_count = int(await session.scalar(
            select(func.count(Invoice.id)).where(Invoice.merchant_id == merchant.id, Invoice.status == "pending")
        ) or 0)
        paid_count = int(await session.scalar(
            select(func.count(Invoice.id)).where(Invoice.merchant_id == merchant.id, Invoice.status == "paid")
        ) or 0)
        other_count = int(await session.scalar(
            select(func.count(Invoice.id)).where(
                Invoice.merchant_id == merchant.id,
                Invoice.status.in_(["expired", "cancelled", "failed", "review"]),
            )
        ) or 0)
        recent = list((await session.scalars(
            select(Invoice)
            .where(Invoice.merchant_id == merchant.id)
            .order_by(Invoice.id.desc())
            .limit(5)
        )).all())

    lines = [
        f"🕓 در انتظار پرداخت: <b>{pending_count:,}</b>",
        f"✅ پرداخت‌های تأییدشده: <b>{paid_count:,}</b>",
        f"📁 سایر وضعیت‌ها: <b>{other_count:,}</b>",
    ]
    if recent:
        lines.extend(["", "<b>آخرین فاکتورها</b>"])
        for invoice in recent:
            title = esc(invoice.description or invoice.order_id)
            lines.append(
                f"• <b>{title}</b> — {money_toman(invoice.payable_amount_rial)} — "
                f"{esc(invoice_status_label(invoice.status))}"
            )
    else:
        lines.extend(["", "هنوز فاکتوری برای این حساب ثبت نشده است."])

    text = panel(
        "🧾",
        "فاکتورهای من",
        lines,
        subtitle="نمای کلی و آخرین وضعیت پرداخت‌ها",
        footer="برای صدور لینک جدید، گزینه «ساخت فاکتور» را انتخاب کنید.",
    )
    await callback.message.edit_text(text, reply_markup=invoices_menu())
    await callback.answer()


@router.callback_query(F.data == "wallet")
async def wallet(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)

    reserve_percent = 0
    if merchant.wallet_balance_rial > 0:
        reserve_percent = round((merchant.reserved_balance_rial / merchant.wallet_balance_rial) * 100)
    fee_label = "رایگان (غیرفعال)" if merchant.verification_fee_rial == 0 else money_toman(merchant.verification_fee_rial)
    footer = (
        "کارمزد این حساب غیرفعال است؛ صدور و تأیید فاکتور بدون نیاز به اعتبار کیف پول انجام می‌شود."
        if merchant.verification_fee_rial == 0
        else "هزینه فقط پس از تأیید قطعی پرداخت کسر می‌شود؛ فاکتور منقضی یا لغوشده هزینه‌ای ندارد."
    )
    text = panel(
        "💰",
        "کیف پول کارمزد",
        [
            f"💵 موجودی کل: <b>{money_toman(merchant.wallet_balance_rial)}</b>",
            f"🔒 اعتبار رزروشده: <b>{money_toman(merchant.reserved_balance_rial)}</b>",
            f"✅ اعتبار قابل استفاده: <b>{money_toman(merchant.available_balance_rial)}</b>",
            f"🧾 هزینه هر تأیید موفق: <b>{fee_label}</b>",
            "",
            f"📊 نسبت اعتبار رزروشده: <b>{reserve_percent}%</b>",
        ],
        subtitle="شارژ و مدیریت اعتبار خدمات پرداخت",
        footer=footer,
    )
    await callback.message.edit_text(text, reply_markup=wallet_menu())
    await callback.answer()


@router.callback_query(F.data == "wallet:topup")
async def wallet_topup_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)
    await callback.message.edit_text(
        panel(
            "➕",
            "شارژ آنلاین کیف پول",
            [
                "مبلغ موردنظر را انتخاب کنید یا مبلغ دلخواه را وارد کنید.",
                "پس از واریز و دریافت پیامک بانک، اعتبار کیف پول به‌صورت خودکار افزایش می‌یابد.",
                "",
                "🔢 کد تطبیق ۱ تا ۹۹۹ تومان به مبلغ افزوده می‌شود و همان مبلغ نیز به اعتبار شما اضافه خواهد شد.",
            ],
            subtitle="شارژ خودکار با تأیید پیامک بانکی",
            footer="حداقل شارژ ۱۰٬۰۰۰ تومان است.",
        ),
        reply_markup=wallet_topup_menu(),
    )
    await callback.answer()


async def _create_wallet_topup(target: Message, user_id: int, amount_toman: int, *, edit: bool) -> None:
    try:
        async with SessionLocal() as session:
            merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == user_id))
            if not merchant:
                raise ValueError("حساب پذیرنده یافت نشد")
            invoice = await create_wallet_topup_invoice(session, merchant, amount_toman * 10)
            card = await session.get(BankCard, invoice.card_id)
            await session.commit()
        payment_url = f"{settings.base_url}/pay/{invoice.token}"
        body = success(
            "درخواست شارژ آماده پرداخت است",
            "\n".join(
                [
                    f"💵 مبلغ انتخابی: <b>{amount_toman:,} تومان</b>",
                    f"🔢 کد تطبیق: <b>+{money_toman(invoice.unique_amount_rial)}</b>",
                    f"💳 مبلغ دقیق واریز: <b>{money_toman(invoice.payable_amount_rial)}</b>",
                    f"💰 اعتبار نهایی قابل افزودن: <b>{money_toman(invoice.payable_amount_rial)}</b>",
                    f"🏦 مقصد: <b>{bank_title(card.bank_code)} •••• {card.card_last4}</b>",
                    f"⏳ وضعیت: <b>{badge('pending')}</b>",
                    "",
                    "🌐 برای پرداخت، از دکمه «پرداخت داخل تلگرام» استفاده کنید.",
                ]
            ),
            footer="پس از تأیید پیامک بانکی، کیف پول به‌صورت خودکار شارژ می‌شود.",
        )
        if edit:
            await target.edit_text(body, reply_markup=wallet_topup_created_menu(payment_url))
        else:
            await target.answer(body, reply_markup=wallet_topup_created_menu(payment_url))
    except Exception as exc:
        body = error(
            "درخواست شارژ ساخته نشد",
            f"{esc(str(exc))}",
            footer="در صورت نبود کارت دریافت سامانه، مدیر باید یک کارت فعال در حساب مدیریتی ثبت کند.",
        )
        if edit:
            await target.edit_text(body, reply_markup=wallet_topup_menu())
        else:
            await target.answer(body, reply_markup=wallet_topup_menu())


@router.callback_query(F.data.startswith("wallet:topup:"))
async def wallet_topup_amount_button(callback: CallbackQuery, state: FSMContext):
    value = callback.data.rsplit(":", 1)[1]
    if value == "custom":
        await state.set_state(WalletTopUpState.amount)
        await callback.message.edit_text(
            panel(
                "✏️",
                "مبلغ دلخواه شارژ",
                ["مبلغ را به تومان و فقط به‌صورت عدد وارد کنید.", "نمونه: <code>350000</code>"],
                footer="حداقل ۱۰٬۰۰۰ و حداکثر ۱۰۰٬۰۰۰٬۰۰۰ تومان",
            ),
            reply_markup=flow_cancel_menu(),
        )
        return await callback.answer()
    try:
        amount_toman = int(value)
    except ValueError:
        return await callback.answer("مبلغ نامعتبر است.", show_alert=True)
    await state.clear()
    await _create_wallet_topup(callback.message, callback.from_user.id, amount_toman, edit=True)
    await callback.answer()


@router.message(WalletTopUpState.amount)
async def wallet_topup_custom_amount(message: Message, state: FSMContext):
    raw = digits_only(message.text)
    if not raw:
        return await message.answer("مبلغ را فقط به‌صورت عدد وارد کنید.", reply_markup=flow_cancel_menu())
    amount_toman = int(raw)
    if amount_toman < 10_000 or amount_toman > 100_000_000:
        return await message.answer(
            "مبلغ باید بین ۱۰٬۰۰۰ تا ۱۰۰٬۰۰۰٬۰۰۰ تومان باشد.",
            reply_markup=flow_cancel_menu(),
        )
    await state.clear()
    await _create_wallet_topup(message, message.from_user.id, amount_toman, edit=False)


@router.callback_query(F.data == "wallet:ledger")
async def wallet_ledger(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        entries = list(
            (
                await session.scalars(
                    select(WalletLedger)
                    .where(WalletLedger.merchant_id == merchant.id)
                    .order_by(WalletLedger.id.desc())
                    .limit(12)
                )
            ).all()
        )

    labels = {
        "wallet_topup": "شارژ آنلاین",
        "admin_credit": "شارژ مدیریتی",
        "deposit": "افزایش اعتبار",
        "admin_debit": "کسر مدیریتی",
        "verification_fee": "کارمزد تأیید",
        "fee_reserved": "رزرو کارمزد",
        "fee_released": "آزادسازی کارمزد",
    }
    lines = []
    for entry in entries:
        sign = "+" if entry.amount_rial >= 0 else "−"
        amount = money_toman(abs(entry.amount_rial))
        created = entry.created_at.strftime("%Y-%m-%d %H:%M") if entry.created_at else "-"
        lines.append(
            f"• <b>{labels.get(entry.entry_type, entry.entry_type)}</b>  {sign}{amount}\n"
            f"  مانده: {money_toman(entry.balance_after_rial)}  •  <code>{created}</code>"
        )
    if not lines:
        lines = ["هنوز تراکنشی در کیف پول ثبت نشده است."]
    await callback.message.edit_text(
        panel(
            "📜",
            "گردش کیف پول",
            lines,
            subtitle="۱۲ رویداد مالی اخیر",
            footer="رزرو کارمزد موجودی را کم نمی‌کند و پس از لغو یا انقضای فاکتور آزاد می‌شود.",
        ),
        reply_markup=wallet_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "fee")
async def fee(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)

    fee_status = "رایگان (غیرفعال)" if merchant.verification_fee_rial == 0 else money_toman(merchant.verification_fee_rial)
    text = panel(
        "⚙️",
        "سیاست پرداخت کارمزد",
        [
            f"هزینه هر تأیید: <b>{fee_status}</b>",
            f"تنظیم فعلی تقسیم هزینه: <b>{fee_title(merchant.fee_mode)}</b>",
            "",
            "👤 <b>مشتری</b> — کل هزینه تأیید به مبلغ فاکتور افزوده می‌شود.",
            "🤝 <b>تقسیم مساوی</b> — نیمی از هزینه به مشتری و نیم دیگر به پذیرنده تعلق می‌گیرد.",
            "🏪 <b>پذیرنده</b> — مبلغ سفارش برای مشتری بدون افزایش باقی می‌ماند.",
        ],
        subtitle="نحوه تقسیم هزینه تأیید هر تراکنش",
        footer=(
            "کارمزد این حساب رایگان است؛ سیاست تقسیم هزینه تا زمان فعال‌شدن دوباره کارمزد اثری ندارد."
            if merchant.verification_fee_rial == 0
            else "این انتخاب، پیش‌فرض فاکتورهای بعدی است و هنگام ساخت هر فاکتور قابل تغییر خواهد بود."
        ),
    )
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
            return await callback.answer("حساب کاربری یافت نشد.", show_alert=True)
        merchant.fee_mode = mode
        await session.commit()

    await callback.message.edit_text(
        success(
            "سیاست کارمزد به‌روزرسانی شد",
            f"روش پیش‌فرض حساب: <b>{fee_title(mode)}</b>",
            footer="این انتخاب از فاکتور بعدی اعمال می‌شود و هنگام صدور هر فاکتور قابل تغییر است.",
        ),
        reply_markup=main_menu(merchant.is_admin),
    )
    await callback.answer("تغییرات ذخیره شد.")


@router.callback_query(F.data == "cards")
async def cards(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)
        total_count = int(await session.scalar(
            select(func.count(BankCard.id)).where(BankCard.merchant_id == merchant.id)
        ) or 0)
        active_count = int(await session.scalar(
            select(func.count(BankCard.id)).where(BankCard.merchant_id == merchant.id, BankCard.is_active.is_(True))
        ) or 0)

    text = panel(
        "🏦",
        "مدیریت کارت‌های مقصد",
        [
            f"💳 کارت‌های ثبت‌شده: <b>{total_count:,}</b>",
            f"🟢 کارت‌های فعال: <b>{active_count:,}</b>",
            "",
            "برای دریافت پرداخت می‌توانید چند کارت از بانک‌های مختلف ثبت کنید.",
            "کارت پیش‌فرض در انتخاب خودکار اولویت دارد و کارت غیرفعال در فاکتورهای جدید استفاده نمی‌شود.",
        ],
        subtitle="ثبت و مشاهده حساب‌های دریافت وجه",
        footer="اطلاعات کامل کارت به‌صورت رمزنگاری‌شده نگهداری می‌شود.",
    )
    await callback.message.edit_text(text, reply_markup=cards_menu())
    await callback.answer()

@router.callback_query(F.data == "card:add")
async def add_card_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddCardState.bank)
    await callback.message.edit_text(
        panel(
            "🏦",
            "ثبت کارت بانکی",
            [progress(1, 4, "انتخاب بانک"), "بانک یا برند صادرکننده کارت را انتخاب کنید."],
            subtitle="مرحله اول از ثبت حساب دریافت وجه",
        ),
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
            panel(
                "🏦",
                "ثبت کارت بانکی",
                [progress(1, 4, "انتخاب بانک"), "نام بانک یا برند بانکی را وارد کنید.", "نمونه: <code>شهر</code>"],
                subtitle="بانک موردنظر در فهرست آماده وجود ندارد",
            ),
            reply_markup=flow_cancel_menu(),
        )
        return await callback.answer()

    await state.update_data(bank=code)
    await state.set_state(AddCardState.card_number)
    await callback.message.edit_text(
        panel(
            "💳",
            "ثبت کارت بانکی",
            [progress(2, 4, "شماره کارت"), f"بانک انتخاب‌شده: <b>{bank_title(code)}</b>", "شماره ۱۶ رقمی کارت را بدون فاصله وارد کنید."],
            subtitle="اطلاعات کارت به‌صورت رمزنگاری‌شده ذخیره می‌شود",
        ),
        reply_markup=flow_cancel_menu(),
    )
    await callback.answer()


@router.message(AddCardState.bank)
async def add_card_bank(message: Message, state: FSMContext):
    raw_bank = " ".join((message.text or "").split()).strip()
    if not raw_bank:
        return await message.answer(
            "نام بانک را به‌صورت متن وارد کنید.", reply_markup=flow_cancel_menu()
        )
    code = normalize_bank_code(raw_bank)
    await state.update_data(bank=code)
    await state.set_state(AddCardState.card_number)
    await message.answer(
        panel(
            "💳",
            "ثبت کارت بانکی",
            [progress(2, 4, "شماره کارت"), f"بانک انتخاب‌شده: <b>{bank_title(code)}</b>", "شماره ۱۶ رقمی کارت را بدون فاصله وارد کنید."],
            subtitle="اطلاعات کارت به‌صورت رمزنگاری‌شده ذخیره می‌شود",
        ),
        reply_markup=flow_cancel_menu(),
    )


@router.message(AddCardState.card_number)
async def add_card_number(message: Message, state: FSMContext):
    number = digits_only(message.text)
    if len(number) != 16:
        return await message.answer(
            warning("شماره کارت معتبر نیست", "شماره کارت باید دقیقاً ۱۶ رقم باشد. لطفاً دوباره وارد کنید."),
            reply_markup=flow_cancel_menu(),
        )

    await state.update_data(card_number=number)
    await state.set_state(AddCardState.holder)
    await message.answer(
        panel(
            "👤",
            "ثبت کارت بانکی",
            [progress(3, 4, "صاحب کارت"), f"کارت: <code>•••• •••• •••• {number[-4:]}</code>", "نام و نام خانوادگی صاحب کارت را وارد کنید."],
        ),
        reply_markup=flow_cancel_menu(),
    )


@router.message(AddCardState.holder)
async def add_card_holder(message: Message, state: FSMContext):
    holder = " ".join((message.text or "").split()).strip()[:120]
    if len(holder) < 3:
        return await message.answer(
            "نام صاحب کارت را به‌صورت متن وارد کنید.", reply_markup=flow_cancel_menu()
        )
    await state.update_data(holder=holder)
    await state.set_state(AddCardState.source_id)
    await message.answer(
        panel(
            "📱",
            "ثبت کارت بانکی",
            [progress(4, 4, "منبع پیامک"), "شناسه دستگاهی را وارد کنید که پیامک این کارت را ارسال می‌کند.", "نمونه: <code>phone-1</code>", "برای تطبیق خودکار بدون محدودیت دستگاه، <code>-</code> ارسال کنید."],
            footer="ثبت شناسه دستگاه، دقت تطبیق چند کارت روی چند گوشی را افزایش می‌دهد.",
        ),
        reply_markup=flow_cancel_menu(),
    )


@router.message(AddCardState.source_id)
async def add_card_source(message: Message, state: FSMContext):
    data = await state.get_data()
    source_text = (message.text or "").strip()
    if not source_text:
        return await message.answer(
            "شناسه دستگاه را وارد کنید یا برای حالت بدون محدودیت علامت <code>-</code> بفرستید.",
            reply_markup=flow_cancel_menu(),
        )
    source_id = None if source_text == "-" else source_text[:120]
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        encryption_key = await get_setting(session, "encryption_key")
        if not merchant or not encryption_key:
            await state.clear()
            return await message.answer("تنظیمات سامانه کامل نیست. لطفاً دوباره تلاش کنید.")

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
        success(
            "کارت مقصد با موفقیت ثبت شد",
            "\n".join([
                f"🏦 بانک: <b>{bank_title(data['bank'])}</b>",
                f"💳 کارت: <code>•••• •••• •••• {data['card_number'][-4:]}</code>",
                f"👤 صاحب کارت: <b>{esc(data['holder'])}</b>",
                f"📱 منبع پیامک: <code>{esc(source_id or 'بدون محدودیت')}</code>",
            ]),
            footer="این کارت از هم‌اکنون برای صدور فاکتور قابل استفاده است.",
        ),
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
        text = panel(
            "💳",
            "کارت‌های مقصد",
            "هنوز کارت بانکی ثبت نشده است.",
            subtitle="حساب‌های دریافت وجه",
            footer="برای شروع، گزینه «افزودن کارت مقصد» را انتخاب کنید.",
        )
    else:
        blocks: list[str] = []
        for card in card_rows:
            tags = []
            if card.is_default:
                tags.append("⭐ پیش‌فرض")
            tags.append("🟢 فعال" if card.is_active else "🔴 غیرفعال")
            blocks.append(
                "\n".join([
                    f"<b>{bank_title(card.bank_code)} •••• {card.card_last4}</b>",
                    f"👤 {esc(card.account_holder)}",
                    f"📱 منبع پیامک: <code>{esc(card.sms_source_id or 'بدون محدودیت')}</code>",
                    f"🏷 {' • '.join(tags)}",
                ])
            )
        text = panel(
            "💳",
            "کارت‌های مقصد",
            "\n\n────────────\n\n".join(blocks),
            subtitle=f"{len(card_rows)} کارت ثبت‌شده",
            footer="شماره کامل کارت فقط در صفحه پرداخت و به‌صورت امن نمایش داده می‌شود.",
        )

    await callback.message.edit_text(text, reply_markup=cards_menu())
    await callback.answer()

@router.callback_query(F.data.startswith("options"))
async def removed_commerce_options(callback: CallbackQuery):
    text, merchant = await home_view(callback.from_user.id)
    await callback.message.edit_text(
        info(
            "این بخش حذف شده است",
            "بلوپی فقط برای اتصال سایت و ربات، صدور فاکتور و تأیید پرداخت استفاده می‌شود. فاکتور دستی از صفحه اصلی در دسترس است.",
        ) + "\n\n" + text,
        reply_markup=main_menu(bool(merchant and merchant.is_admin)),
    )
    await callback.answer("مرکز آپشن‌ها حذف شده است.")


@router.callback_query(F.data == "flow:cancel")
async def cancel_flow(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text, merchant = await home_view(callback.from_user.id)
    await callback.message.edit_text(
        info("فرایند لغو شد", "اطلاعات واردشده در این مرحله ذخیره نشد.") + "\n\n" + text,
        reply_markup=main_menu(bool(merchant and merchant.is_admin)),
    )
    await callback.answer("فرایند لغو شد.")

@router.callback_query(F.data.in_({"invoice:new", "invoice:restart"}))
async def invoice_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(
        manual_order_id=f"MANUAL-TG-{callback.from_user.id}-{secrets.token_hex(6).upper()}"
    )
    await state.set_state(ManualInvoiceState.amount)
    await callback.message.edit_text(
        panel(
            "🧾",
            "ایجاد فاکتور دستی",
            [
                progress(1, 4, "مبلغ سفارش"),
                "مبلغ پایه سفارش را به <b>تومان</b> وارد کنید.",
                "نمونه: <code>200000</code>",
            ],
            subtitle="ساخت لینک پرداخت اختصاصی برای مشتری",
            footer="کد تطبیق یکتا و سهم کارمزد در مرحله نهایی به مبلغ اضافه می‌شود.",
        ),
        reply_markup=flow_cancel_menu(),
    )
    await callback.answer()

@router.message(ManualInvoiceState.amount)
async def invoice_amount(message: Message, state: FSMContext):
    raw = digits_only(message.text)
    if not raw:
        return await message.answer(
            warning("مبلغ معتبر نیست", "مبلغ سفارش را به‌صورت عددی و به تومان وارد کنید."),
            reply_markup=flow_cancel_menu(),
        )

    amount = int(raw)
    if amount < 1_000 or amount > 500_000_000:
        return await message.answer(
            warning(
                "مبلغ خارج از محدوده است",
                "مبلغ سفارش باید بین ۱٬۰۰۰ تا ۵۰۰٬۰۰۰٬۰۰۰ تومان باشد.",
            ),
            reply_markup=flow_cancel_menu(),
        )
    await state.update_data(amount_toman=amount)
    await state.set_state(ManualInvoiceState.description)
    await message.answer(
        panel(
            "📝",
            "ایجاد فاکتور دستی",
            [progress(2, 4, "عنوان سفارش"), f"مبلغ پایه: <b>{amount:,} تومان</b>", "عنوانی کوتاه و قابل‌فهم برای مشتری وارد کنید.", "نمونه: <code>اشتراک یک‌ماهه</code>"],
        ),
        reply_markup=flow_cancel_menu(),
    )


@router.message(ManualInvoiceState.description)
async def invoice_description(message: Message, state: FSMContext):
    description = " ".join((message.text or "").split()).strip()[:500]
    if not description:
        return await message.answer("عنوان فاکتور الزامی است.", reply_markup=flow_cancel_menu())

    await state.update_data(description=description)
    merchant = await current_merchant(message.from_user.id)
    if not merchant:
        await state.clear()
        return await message.answer("حساب پذیرنده یافت نشد. لطفاً دستور /start را ارسال کنید.")

    if merchant.verification_fee_rial == 0:
        await state.update_data(fee_mode=merchant.fee_mode)
        await message.answer(
            info(
                "کارمزد این حساب رایگان است",
                "مرحله تقسیم هزینه حذف شد و فاکتور بدون رزرو یا کسر اعتبار صادر می‌شود.",
            )
        )
        return await show_invoice_cards(message, state, message.from_user.id)

    await state.set_state(ManualInvoiceState.fee_mode)
    await message.answer(
        panel(
            "⚙️",
            "ایجاد فاکتور دستی",
            [progress(3, 4, "تقسیم هزینه"), "مشخص کنید هزینه تأیید این فاکتور توسط چه کسی پرداخت شود.", f"پیش‌فرض حساب: <b>{fee_title(merchant.fee_mode)}</b>"],
        ),
        reply_markup=invoice_fee_mode_menu(merchant.fee_mode),
    )


@router.message(ManualInvoiceState.fee_mode)
async def invoice_fee_mode_text(message: Message, state: FSMContext):
    mode = (message.text or "").strip().lower()
    merchant = await current_merchant(message.from_user.id)
    if not merchant:
        await state.clear()
        return await message.answer("حساب پذیرنده یافت نشد.")
    if mode not in {"customer", "split", "merchant", "default"}:
        return await message.answer(
            "لطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
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
            "⚠️ <b>کارت فعالی پیدا نشد</b>\n\nابتدا از بخش «کارت‌های من» یک کارت بانکی ثبت کنید.",
            reply_markup=cards_menu(),
        )
        return

    await state.set_state(ManualInvoiceState.card)
    keyboard_data = [(c.id, bank_title(c.bank_code), c.card_last4, c.is_default) for c in cards]
    await target.answer(
        panel(
            "🏦",
            "ایجاد فاکتور دستی",
            [progress(4, 4, "کارت مقصد"), "کارت دریافت‌کننده وجه را انتخاب کنید.", "انتخاب هوشمند، کارت پیش‌فرض و اولویت‌های حساب را در نظر می‌گیرد."],
        ),
        reply_markup=invoice_cards_menu(keyboard_data),
    )


@router.callback_query(F.data.startswith("invoice:fee:"))
async def invoice_fee_mode_button(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != ManualInvoiceState.fee_mode.state:
        return await callback.answer("فرایند ساخت فاکتور منقضی شده؛ لطفاً فرایند را دوباره آغاز کنید.", show_alert=True)

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
            "⚠️ <b>کارت فعالی پیدا نشد</b>\n\nابتدا یک کارت بانکی ثبت کنید.",
            reply_markup=cards_menu(),
        )
        return await callback.answer()

    await state.set_state(ManualInvoiceState.card)
    keyboard_data = [(c.id, bank_title(c.bank_code), c.card_last4, c.is_default) for c in cards]
    await callback.message.edit_text(
        panel(
            "🏦",
            "ایجاد فاکتور دستی",
            [progress(4, 4, "کارت مقصد"), "کارت دریافت‌کننده وجه را انتخاب کنید."],
        ),
        reply_markup=invoice_cards_menu(keyboard_data),
    )
    await callback.answer()


@router.message(ManualInvoiceState.card)
async def invoice_card_text(message: Message, state: FSMContext):
    raw = digits_only(message.text)
    if not raw:
        return await message.answer("کارت مقصد را از فهرست انتخاب کنید یا شناسه عددی آن را وارد کنید.")
    await prepare_invoice_preview(message, state, message.from_user.id, int(raw))


@router.callback_query(F.data.startswith("invoice:card:"))
async def invoice_card_button(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != ManualInvoiceState.card.state:
        return await callback.answer("فرایند ساخت فاکتور منقضی شده؛ لطفاً فرایند را دوباره آغاز کنید.", show_alert=True)

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
            return await target.answer(error("حساب پذیرنده یافت نشد", "دوباره وارد پنل شوید و فرایند را از ابتدا آغاز کنید."))

        stmt = select(BankCard).where(BankCard.merchant_id == merchant.id, BankCard.is_active.is_(True))
        if requested_card_id:
            stmt = stmt.where(BankCard.id == requested_card_id)
        else:
            stmt = stmt.order_by(BankCard.is_default.desc(), BankCard.priority.asc(), BankCard.id.asc())
        card = await session.scalar(stmt.limit(1))

    if not card:
        return await target.answer(
            warning("کارت مقصد در دسترس نیست", "کارت انتخاب‌شده حذف یا غیرفعال شده است. کارت دیگری انتخاب کنید."),
            reply_markup=flow_cancel_menu(),
        )

    mode = data.get("fee_mode") or merchant.fee_mode
    fee_rial = merchant.verification_fee_rial
    customer_fee_rial = calculate_customer_fee(fee_rial, mode)
    base_rial = data["amount_toman"] * 10
    nominal_payable_rial = base_rial + customer_fee_rial

    await state.update_data(resolved_fee_mode=mode, selected_card_id=card.id)
    await state.set_state(ManualInvoiceState.confirm)

    preview = panel(
        "🧾",
        "بررسی و تأیید فاکتور",
        [
            f"📝 عنوان سفارش: <b>{esc(data['description'])}</b>",
            f"💵 مبلغ پایه: <b>{money_toman(base_rial)}</b>",
            f"🧾 هزینه تأیید: <b>{'رایگان' if fee_rial == 0 else money_toman(fee_rial)}</b>",
            f"👤 سهم مشتری از هزینه: <b>{money_toman(customer_fee_rial)}</b>",
            f"🔢 کد تطبیق یکتا: <b>۱ تا ۹۹۹ تومان</b>",
            f"💳 مبلغ پیش از کد تطبیق: <b>{money_toman(nominal_payable_rial)}</b>",
            "",
            f"🏦 کارت مقصد: <b>{bank_title(card.bank_code)} •••• {card.card_last4}</b>",
            f"⚙️ سیاست کارمزد: <b>{fee_title(mode)}</b>",
            f"⏱ اعتبار لینک: <b>{settings.invoice_ttl_minutes} دقیقه</b>",
        ],
        subtitle="آخرین مرحله پیش از صدور لینک پرداخت",
        footer=(
            "این فاکتور بدون کارمزد صادر می‌شود و اعتباری از کیف پول رزرو نخواهد شد."
            if fee_rial == 0
            else "پس از صدور، هزینه تأیید در کیف پول رزرو می‌شود و در صورت لغو یا انقضا آزاد خواهد شد."
        ),
    )

    if edit:
        await target.edit_text(preview, reply_markup=invoice_confirm_menu())
    else:
        await target.answer(preview, reply_markup=invoice_confirm_menu())

@router.callback_query(F.data == "invoice:confirm")
async def invoice_confirm(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != ManualInvoiceState.confirm.state:
        return await callback.answer("اطلاعات فاکتور منقضی شده است؛ دوباره شروع کنید.", show_alert=True)

    data = await state.get_data()
    manual_order_id = data.get("manual_order_id") or (
        f"MANUAL-TG-{callback.from_user.id}-{secrets.token_hex(6).upper()}"
    )
    await state.update_data(manual_order_id=manual_order_id)
    await state.set_state(ManualInvoiceState.processing)

    invoice = None
    card = None
    try:
        async with SessionLocal() as session:
            merchant = await session.scalar(
                select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id)
            )
            if not merchant:
                raise ValueError("حساب پذیرنده یافت نشد")

            # The stable order id makes confirmation idempotent. A double tap,
            # Telegram retry or message-edit failure must never create a second
            # payable invoice for the same manual flow.
            invoice = await session.scalar(
                select(Invoice).where(
                    Invoice.merchant_id == merchant.id,
                    Invoice.order_id == manual_order_id,
                )
            )
            if invoice is None:
                invoice = await create_invoice(
                    session,
                    merchant,
                    base_amount_rial=data["amount_toman"] * 10,
                    description=data["description"],
                    order_id=manual_order_id,
                    fee_mode=data["resolved_fee_mode"],
                    card_id=data["selected_card_id"],
                    source_channel="telegram_bot",
                )
            card = await session.get(BankCard, invoice.card_id)
            await session.commit()
    except IntegrityError:
        # A concurrent confirmation may win the unique order constraint. Read
        # and return that invoice instead of reporting failure or creating one
        # with a new order id.
        async with SessionLocal() as session:
            merchant = await session.scalar(
                select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id)
            )
            if merchant:
                invoice = await session.scalar(
                    select(Invoice).where(
                        Invoice.merchant_id == merchant.id,
                        Invoice.order_id == manual_order_id,
                    )
                )
                if invoice:
                    card = await session.get(BankCard, invoice.card_id)
        if invoice is None:
            await state.set_state(ManualInvoiceState.confirm)
            await callback.answer("صدور فاکتور ناموفق بود؛ دوباره تلاش کنید.", show_alert=True)
            return
    except ValueError as exc:
        await state.set_state(ManualInvoiceState.confirm)
        await callback.answer("صدور فاکتور انجام نشد.", show_alert=True)
        await callback.message.edit_text(
            error(
                "فاکتور صادر نشد",
                esc(str(exc)),
                footer="اطلاعات واردشده حفظ شده است؛ پس از رفع مشکل دوباره تأیید کنید.",
            ),
            reply_markup=invoice_confirm_menu(),
        )
        return
    except Exception as exc:
        print(
            f"manual_invoice_create_error order_id={manual_order_id} "
            f"{type(exc).__name__}: {exc}"
        )
        await state.set_state(ManualInvoiceState.confirm)
        await callback.answer("صدور فاکتور ناموفق بود.", show_alert=True)
        await callback.message.edit_text(
            error(
                "فاکتور صادر نشد",
                "خطای داخلی هنگام صدور فاکتور رخ داد. دوباره تلاش کنید.",
                footer="اطلاعات واردشده حفظ شده است و فاکتور تکراری ساخته نخواهد شد.",
            ),
            reply_markup=invoice_confirm_menu(),
        )
        return

    await state.clear()
    payment_url = f"{settings.base_url}/pay/{invoice.token}"
    card_label = (
        f"{bank_title(card.bank_code)} •••• {card.card_last4}"
        if card
        else "کارت مقصد ثبت‌شده"
    )
    text = success(
        "فاکتور آماده دریافت وجه است",
        "\n".join([
            f"📝 عنوان: <b>{esc(invoice.description or '-')}</b>",
            f"🧾 شناسه پرداخت: <code>{invoice.token}</code>",
            f"💳 مبلغ دقیق واریز: <b>{money_toman(invoice.payable_amount_rial)}</b>",
            f"🔢 کد تطبیق: <b>+{money_toman(invoice.unique_amount_rial)}</b>",
            f"🏦 مقصد: <b>{esc(card_label)}</b>",
            f"⏳ وضعیت: <b>{badge(invoice.status)}</b>",
            "",
            "🌐 برای مشاهده صفحه پرداخت، از دکمه «بازکردن داخل تلگرام» استفاده کنید.",
        ]),
        footer="برای ارسال به مشتری، دکمه «کپی لینک پرداخت» را بزنید. وضعیت پرداخت پس از دریافت پیامک بانک به‌صورت خودکار به‌روزرسانی می‌شود.",
    )

    try:
        await callback.message.edit_text(text, reply_markup=payment_created_menu(payment_url))
    except Exception as exc:
        # The invoice is already committed. A stale Telegram message must not
        # turn a successful payment request into a retryable creation failure.
        print(f"manual_invoice_message_edit_error={type(exc).__name__}: {exc}")
        await callback.message.answer(text, reply_markup=payment_created_menu(payment_url))
    try:
        await callback.answer("فاکتور صادر شد.")
    except Exception:
        pass

@router.callback_query(F.data == "connect")
async def connection_panel(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)
        store_count = int(await session.scalar(
            select(func.count(Store.id)).where(Store.merchant_id == merchant.id, Store.is_active.is_(True))
        ) or 0)
        key_count = int(await session.scalar(
            select(func.count(StoreApiKey.id))
            .join(Store, Store.id == StoreApiKey.store_id)
            .where(
                Store.merchant_id == merchant.id,
                Store.is_active.is_(True),
                StoreApiKey.is_active.is_(True),
            )
        ) or 0)
        store_callback_count = int(await session.scalar(
            select(func.count(Store.id)).where(
                Store.merchant_id == merchant.id,
                Store.is_active.is_(True),
                Store.callback_url.is_not(None),
            )
        ) or 0)
    docs_url = merchant_docs_url(merchant)
    api_ready = store_count > 0 and key_count == store_count
    callback_ready = store_count > 0 and store_callback_count == store_count
    ready_count = int(api_ready) + int(callback_ready) + 1
    text = panel(
        "🔌",
        "مرکز اتصال و توسعه‌دهندگان",
        [
            f"📈 پیشرفت راه‌اندازی: <b>{ready_count} از 3 سرویس</b>",
            "",
            f"🏪 فروشگاه‌های فعال: <b>{store_count:,}</b>",
            f"🔑 APIهای فعال: <b>{key_count:,}</b>",
            f"📲 دریافت پیامک بانکی: <b>{badge('ready')}</b>",
            f"🔔 Callbackهای تنظیم‌شده: <b>{store_callback_count:,} از {store_count:,}</b>",
            "",
            "<b>اتصال پیشنهادی</b>",
            "1️⃣ فروشگاه خود را ثبت کنید.",
            "2️⃣ کلید API اختصاصی همان فروشگاه را در Backend ذخیره کنید.",
            "3️⃣ برای هر فروشگاه Callback اختصاصی آن را ثبت و آزمایش کنید.",
            "4️⃣ کلید هر فروشگاه را فقط در Backend همان پروژه نگه دارید.",
            "",
            f"📘 مستندات عمومی API:\n<code>{esc(docs_url)}</code>",
        ],
        subtitle="مدیریت چند فروشگاه با API و Callback مستقل",
        footer="وبهوک پیامک در سطح پذیرنده مشترک است؛ API و Callback برای هر فروشگاه جدا مدیریت می‌شوند.",
    )
    await callback.message.edit_text(text, reply_markup=connection_menu(docs_url, merchant_portal_url(merchant)))
    await callback.answer()


@router.callback_query(F.data == "api")
async def api_panel(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)
    docs_url = merchant_docs_url(merchant)
    text = panel(
        "🔑",
        "API فروشگاهی",
        [
            f"🌐 آدرس پایه: <code>{settings.base_url}/api/v1</code>",
            "",
            "هر فروشگاه دقیقاً یک کلید API اختصاصی و یک Callback مستقل دارد.",
            "با بازنشانی کلید، کلید قبلی همان فروشگاه فوراً باطل و کلید جدید جایگزین می‌شود.",
            "",
            "<b>هدر احراز هویت</b>",
            "<code>X-API-Key: gw_your_private_key</code>",
            "",
            "فاکتورهای ساخته‌شده با هر کلید به همان فروشگاه متصل می‌شوند و کلید فروشگاه دیگری امکان مشاهده یا لغو آن‌ها را ندارد.",
        ],
        subtitle="جداسازی امن دسترسی‌ها برای هر کسب‌وکار",
        footer="هر فروشگاه یک کلید دارد؛ برای تعویض امن، همان کلید را بازنشانی کنید.",
    )
    await callback.message.edit_text(text, reply_markup=api_menu(docs_url))
    await callback.answer()


async def _render_stores(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        stores = await list_merchant_stores(session, merchant.id)
    rows = [
        (store.id, store.name, store.code, store.is_active, sum(1 for key in store.api_keys if key.is_active))
        for store in stores
    ]
    active_stores = sum(1 for store in stores if store.is_active)
    active_keys = sum(sum(1 for key in store.api_keys if key.is_active) for store in stores if store.is_active)
    lines = [
        f"🏪 تعداد فروشگاه‌ها: <b>{len(stores):,}</b>",
        f"✅ فروشگاه فعال: <b>{active_stores:,}</b>",
        f"🔑 API فعال: <b>{active_keys:,}</b>",
        "",
        "هر فروشگاه یک API اختصاصی، یک Callback و یک Secret امضای مستقل دارد.",
    ]
    if not stores:
        lines.extend(["", "هنوز فروشگاهی ثبت نشده است؛ برای شروع یک فروشگاه ایجاد کنید."])
    await callback.message.edit_text(
        panel(
            "🏪",
            "فروشگاه‌ها و دسترسی‌های API",
            lines,
            subtitle="ثبت و مدیریت چند کسب‌وکار در یک حساب پذیرنده",
            footer="حداکثر ۲۰ فروشگاه قابل ثبت است و هر فروشگاه فقط یک کلید API اختصاصی دارد.",
        ),
        reply_markup=stores_menu(rows),
    )
    await callback.answer()


@router.callback_query(F.data == "stores")
async def stores_panel(callback: CallbackQuery):
    await _render_stores(callback)


@router.callback_query(F.data == "store:add")
async def store_add_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(StoreCreateState.name)
    await callback.message.edit_text(
        panel(
            "🏪",
            "ثبت فروشگاه جدید",
            [
                "نام فروشگاه یا پروژه‌ای که پرداخت‌های آن از این API انجام می‌شود را وارد کنید.",
                "نمونه: <code>فروشگاه اینترنتی رویال</code>",
            ],
            subtitle="مرحله ۱ از ۲ • مشخصات فروشگاه",
            footer="هم‌زمان با ثبت فروشگاه، یک کلید API اختصاصی و Secret مستقل ساخته می‌شود.",
        ),
        reply_markup=flow_cancel_menu(),
    )
    await callback.answer()


@router.message(StoreCreateState.name)
async def store_add_name(message: Message, state: FSMContext):
    name = " ".join((message.text or "").split()).strip()
    if len(name) < 2 or len(name) > 120:
        return await message.answer("نام فروشگاه باید بین ۲ تا ۱۲۰ نویسه باشد.", reply_markup=flow_cancel_menu())
    await state.update_data(store_name=name)
    await state.set_state(StoreCreateState.website)
    await message.answer(
        panel(
            "🌐",
            "نشانی فروشگاه",
            [
                "نشانی HTTPS سایت یا پنل فروشگاه را ارسال کنید.",
                "نمونه: <code>https://shop.example.com</code>",
                "",
                "اگر فروشگاه شما سایت ندارد، یک خط تیره <code>-</code> ارسال کنید.",
            ],
            subtitle="مرحله ۲ از ۲ • نشانی اختیاری",
        ),
        reply_markup=flow_cancel_menu(),
    )


@router.message(StoreCreateState.website)
async def store_add_website(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    website_url: str | None = None
    if raw != "-":
        candidate = raw if "://" in raw else f"https://{raw}"
        valid, normalized = validate_public_https_url(candidate)
        if not valid:
            return await message.answer(
                warning("نشانی فروشگاه معتبر نیست", esc(normalized), footer="نشانی HTTPS عمومی یا علامت - را ارسال کنید."),
                reply_markup=flow_cancel_menu(),
            )
        website_url = normalized
    data = await state.get_data()
    try:
        async with SessionLocal() as session:
            merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
            if not merchant:
                raise ValueError("حساب پذیرنده یافت نشد")
            store = await create_store(session, merchant, data.get("store_name", "فروشگاه"), website_url)
            key_record, plain_key = await issue_store_api_key(session, store, "کلید اصلی")
            await session.commit()
            store_id = store.id
            store_code = store.code
            store_name = store.name
            secret = store.callback_secret
        await state.clear()
        await message.answer(
            success(
                "فروشگاه و کلید API اختصاصی ساخته شد",
                "\n".join([
                    f"🏪 فروشگاه: <b>{esc(store_name)}</b>",
                    f"🪪 کد فروشگاه: <code>{esc(store_code)}</code>",
                    f"🔑 شناسه کلید: <code>{key_record.id}</code>",
                    "",
                    "<b>کلید محرمانه؛ فقط همین یک‌بار نمایش داده می‌شود</b>",
                    f"<code>{plain_key}</code>",
                    "",
                    "هدر درخواست:",
                    "<code>X-API-Key: YOUR_KEY</code>",
                    "",
                    f"🔐 Secret امضای Callback:\n<code>{esc(secret)}</code>",
                ]),
                footer="کلید را در Backend فروشگاه ذخیره کنید؛ قرار دادن آن در کد فرانت‌اند یا کانال عمومی ناامن است.",
            ),
            reply_markup=store_detail_menu(
                store_id,
                active=True,
                callback_configured=False,
                keys=[(key_record.id, key_record.label, key_record.key_prefix, True)],
            ),
        )
    except ValueError as exc:
        await state.clear()
        await message.answer(error("ثبت فروشگاه انجام نشد", esc(str(exc))), reply_markup=main_menu(False))


async def _render_store_detail(callback: CallbackQuery, store_id: int, *, notice: str | None = None) -> None:
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        store = await get_owned_store(session, merchant.id, store_id, with_keys=True)
        if not store:
            return await callback.answer("فروشگاه یافت نشد.", show_alert=True)
        invoice_count = int(await session.scalar(
            select(func.count(Invoice.id)).where(Invoice.store_id == store.id)
        ) or 0)
        paid_count = int(await session.scalar(
            select(func.count(Invoice.id)).where(Invoice.store_id == store.id, Invoice.status == "paid")
        ) or 0)
        all_keys = sorted(store.api_keys, key=lambda item: (not item.is_active, item.id))
        keys = all_keys[:1]
        values = {
            "id": store.id,
            "name": store.name,
            "code": store.code,
            "website": store.website_url,
            "callback": store.callback_url,
            "secret": store.callback_secret,
            "active": store.is_active,
        }
    lines = [
        f"📡 وضعیت: <b>{badge('active' if values['active'] else 'inactive')}</b>",
        f"🪪 کد فروشگاه: <code>{esc(values['code'])}</code>",
        f"🌐 نشانی: <code>{esc(values['website'] or 'ثبت نشده')}</code>",
        f"🔔 Callback: <code>{esc(values['callback'] or 'تنظیم نشده')}</code>",
        f"🔐 Secret امضا: <code>{esc(values['secret'])}</code>",
        "",
        f"🔑 وضعیت API: <b>{'فعال' if keys and keys[0].is_active else 'غیرفعال'}</b>",
        f"🧾 کل فاکتورها: <b>{invoice_count:,}</b>",
        f"✅ پرداخت‌های موفق: <b>{paid_count:,}</b>",
    ]
    if notice:
        lines.extend(["", f"✅ {esc(notice)}"])
    await callback.message.edit_text(
        panel(
            "🏪",
            values["name"],
            lines,
            subtitle="تنظیمات اتصال و دسترسی‌های فروشگاه",
            footer="هر فروشگاه فقط یک API دارد؛ بازنشانی کلید، دسترسی قبلی را فوراً باطل می‌کند.",
        ),
        reply_markup=store_detail_menu(
            store_id,
            active=values["active"],
            callback_configured=bool(values["callback"]),
            keys=[(key.id, key.label, key.key_prefix, key.is_active) for key in keys],
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("store:view:"))
async def store_view(callback: CallbackQuery):
    await _render_store_detail(callback, int(callback.data.rsplit(":", 1)[1]))


@router.callback_query(F.data.startswith("store:key:new:"))
async def store_key_new(callback: CallbackQuery):
    """Create the single API key only for stores that do not have one yet."""
    store_id = int(callback.data.rsplit(":", 1)[1])
    try:
        async with SessionLocal() as session:
            merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
            if not merchant:
                raise ValueError("حساب پذیرنده یافت نشد")
            store = await get_owned_store(session, merchant.id, store_id, with_keys=True)
            if not store:
                raise ValueError("فروشگاه یافت نشد")
            if not store.is_active:
                raise ValueError("برای ساخت API، ابتدا فروشگاه را فعال کنید")
            key_record, plain_key = await issue_store_api_key(session, store, "کلید اصلی")
            await session.commit()
        await callback.message.answer(
            success(
                "کلید API فروشگاه ساخته شد",
                "\n".join([
                    f"🏪 فروشگاه: <b>{esc(store.name)}</b>",
                    f"🪪 شناسه کلید: <code>{key_record.id}</code>",
                    "",
                    "<b>کلید محرمانه؛ فقط همین یک‌بار نمایش داده می‌شود</b>",
                    f"<code>{plain_key}</code>",
                ]),
                footer="این فروشگاه فقط یک کلید API دارد. برای تعویض، از گزینه بازنشانی کلید استفاده کنید.",
            )
        )
        await _render_store_detail(callback, store_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)


@router.callback_query(F.data.startswith("store:key:rotate:"))
async def store_key_rotate(callback: CallbackQuery):
    store_id = int(callback.data.rsplit(":", 1)[1])
    try:
        async with SessionLocal() as session:
            merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
            if not merchant:
                raise ValueError("حساب پذیرنده یافت نشد")
            store = await get_owned_store(session, merchant.id, store_id, with_keys=True)
            if not store:
                raise ValueError("فروشگاه یافت نشد")
            if not store.is_active:
                raise ValueError("برای بازنشانی API، ابتدا فروشگاه را فعال کنید")
            key_record, plain_key = await rotate_store_api_key(session, store)
            await session.commit()
        await callback.message.answer(
            success(
                "کلید API فروشگاه بازنشانی شد",
                "\n".join([
                    f"🏪 فروشگاه: <b>{esc(store.name)}</b>",
                    f"🪪 شناسه کلید: <code>{key_record.id}</code>",
                    "",
                    "<b>کلید جدید؛ فقط همین یک‌بار نمایش داده می‌شود</b>",
                    f"<code>{plain_key}</code>",
                ]),
                footer="کلید قبلی فوراً باطل شد؛ کلید جدید را در Backend همان فروشگاه جایگزین کنید.",
            )
        )
        await _render_store_detail(callback, store_id, notice="کلید API با موفقیت بازنشانی شد.")
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)


@router.callback_query(F.data.startswith("store:key:toggle:"))
async def store_key_toggle(callback: CallbackQuery):
    _, _, _, key_id_raw, store_id_raw = callback.data.split(":")
    key_id, store_id = int(key_id_raw), int(store_id_raw)
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        key = await session.get(StoreApiKey, key_id)
        if not key or key.merchant_id != merchant.id or key.store_id != store_id:
            return await callback.answer("کلید API یافت نشد.", show_alert=True)
        new_active = not key.is_active
        try:
            await set_key_active(session, merchant.id, key.id, new_active)
        except ValueError as exc:
            return await callback.answer(str(exc), show_alert=True)
        status = "فعال" if new_active else "غیرفعال"
        await session.commit()
    await _render_store_detail(callback, store_id, notice=f"کلید API {status} شد.")


@router.callback_query(F.data.startswith("store:toggle:"))
async def store_toggle(callback: CallbackQuery):
    store_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        store = await get_owned_store(session, merchant.id, store_id)
        if not store:
            return await callback.answer("فروشگاه یافت نشد.", show_alert=True)
        store.is_active = not store.is_active
        active = store.is_active
        await session.commit()
    await _render_store_detail(callback, store_id, notice="فروشگاه فعال شد." if active else "فروشگاه غیرفعال شد.")


@router.callback_query(F.data.startswith("store:callback:set:"))
async def store_callback_set_start(callback: CallbackQuery, state: FSMContext):
    store_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        store = await get_owned_store(session, merchant.id, store_id) if merchant else None
        if not store:
            return await callback.answer("فروشگاه یافت نشد.", show_alert=True)
    await state.clear()
    await state.update_data(store_id=store_id)
    await state.set_state(StoreCallbackState.url)
    await callback.message.edit_text(
        panel(
            "🔔",
            "Callback اختصاصی فروشگاه",
            [
                f"🏪 فروشگاه: <b>{esc(store.name)}</b>",
                "نشانی HTTPS عمومی دریافت نتیجه پرداخت را ارسال کنید.",
                "نمونه: <code>https://shop.example.com/api/payment/callback</code>",
            ],
            subtitle="رویدادهای این فروشگاه از سایر فروشگاه‌ها جدا ارسال می‌شوند",
            footer="Secret امضای اختصاصی فروشگاه در صفحه جزئیات نمایش داده می‌شود.",
        ),
        reply_markup=flow_cancel_menu(),
    )
    await callback.answer()


@router.message(StoreCallbackState.url)
async def store_callback_set_value(message: Message, state: FSMContext):
    valid, normalized = validate_public_https_url((message.text or "").strip())
    if not valid:
        return await message.answer(
            warning("نشانی Callback معتبر نیست", esc(normalized), footer="یک نشانی HTTPS عمومی وارد کنید."),
            reply_markup=flow_cancel_menu(),
        )
    data = await state.get_data()
    store_id = int(data.get("store_id", 0))
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        store = await get_owned_store(session, merchant.id, store_id) if merchant else None
        if not store:
            await state.clear()
            return await message.answer(error("فروشگاه یافت نشد", "فرایند را دوباره آغاز کنید."))
        store.callback_url = normalized
        if not store.callback_secret:
            store.callback_secret = random_secret(32)
        await session.commit()
        secret = store.callback_secret
        name = store.name
    await state.clear()
    await message.answer(
        success(
            "Callback فروشگاه ثبت شد",
            f"🏪 فروشگاه: <b>{esc(name)}</b>\n🌐 نشانی:\n<code>{esc(normalized)}</code>\n\n🔐 Secret:\n<code>{esc(secret)}</code>",
            footer="از صفحه فروشگاه، رویداد آزمایشی ارسال کنید.",
        ),
        reply_markup=main_menu(bool(merchant and merchant.is_admin)),
    )


@router.callback_query(F.data.startswith("store:callback:remove:"))
async def store_callback_remove(callback: CallbackQuery):
    store_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        store = await get_owned_store(session, merchant.id, store_id) if merchant else None
        if not store:
            return await callback.answer("فروشگاه یافت نشد.", show_alert=True)
        store.callback_url = None
        await session.commit()
    await _render_store_detail(callback, store_id, notice="Callback فروشگاه حذف شد.")


@router.callback_query(F.data.startswith("store:secret:"))
async def store_secret_regen(callback: CallbackQuery):
    store_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        store = await get_owned_store(session, merchant.id, store_id) if merchant else None
        if not store:
            return await callback.answer("فروشگاه یافت نشد.", show_alert=True)
        store.callback_secret = random_secret(32)
        secret = store.callback_secret
        await session.commit()
    await callback.message.answer(
        warning(
            "Secret فروشگاه بازنشانی شد",
            f"Secret جدید:\n<code>{esc(secret)}</code>",
            footer="Secret قبلی برای رویدادهای جدید معتبر نیست؛ فاکتورهای در انتظار، Secret ذخیره‌شده زمان صدور را حفظ می‌کنند.",
        )
    )
    await _render_store_detail(callback, store_id)


@router.callback_query(F.data.startswith("store:callback:test:"))
async def store_callback_test(callback: CallbackQuery):
    store_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        store = await get_owned_store(session, merchant.id, store_id) if merchant else None
        if not store or not store.callback_url:
            return await callback.answer("ابتدا Callback فروشگاه را ثبت کنید.", show_alert=True)
    await callback.answer("در حال آزمایش اتصال…")
    ok, result = await send_store_test_callback(store)
    await callback.message.answer(
        success("آزمایش Callback فروشگاه موفق بود", f"پاسخ مقصد: <code>{esc(result)}</code>")
        if ok else
        error("آزمایش Callback فروشگاه ناموفق بود", f"نتیجه: <code>{esc(result)}</code>")
    )


@router.callback_query(F.data == "api:regen")
async def api_regen_legacy(callback: CallbackQuery):
    """Compatibility for old keyboards: rotate the first active store key."""
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        store = await session.scalar(
            select(Store).where(Store.merchant_id == merchant.id, Store.is_active.is_(True)).order_by(Store.id.asc())
        )
        if not store:
            return await callback.answer("ابتدا یک فروشگاه ثبت کنید.", show_alert=True)
        try:
            key_record, plain_key = await rotate_store_api_key(session, store)
            await session.commit()
        except ValueError as exc:
            return await callback.answer(str(exc), show_alert=True)
    await callback.message.answer(
        success(
            "کلید API فروشگاه بازنشانی شد",
            f"🏪 {esc(store.name)}\n🏷 {esc(key_record.label)}\n\n<code>{plain_key}</code>",
            footer="کلید قبلی این فروشگاه فوراً باطل شد و کلید جدید فقط همین یک‌بار نمایش داده می‌شود.",
        )
    )
    await callback.answer("کلید فروشگاه بازنشانی شد.")


@router.callback_query(F.data == "sms:webhook")
async def sms_info(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)
    webhook_url = merchant_sms_webhook_url(merchant)
    docs_url = merchant_docs_url(merchant)
    payload = '{"device_id":"phone-1","sender":"{in-number}","message":"{msg}","received_time":"{time}","incoming_sim":"{in-sim}"}'
    text = panel(
        "📲",
        "وبهوک اختصاصی پیامک بانکی",
        [
            "<b>نشانی محرمانه وبهوک</b>",
            f"<code>{esc(webhook_url)}</code>",
            "",
            "<b>تنظیم درخواست</b>",
            "• Method: <code>POST</code>",
            "• Body: <code>JSON</code>",
            "• Header دستی: نیاز نیست",
            "",
            "<b>بدنه استاندارد</b>",
            f"<code>{esc(payload)}</code>",
            "",
            "<b>راهنمای کوتاه</b>",
            "1️⃣ متغیرهای <code>{in-number}</code> و <code>{msg}</code> را با دکمه <b>{}</b> خود برنامه درج کنید.",
            "2️⃣ فقط یک نوع پیام، ترجیحاً SMS، برای هر فیلتر فعال باشد.",
            "3️⃣ تنظیم را ذخیره و با یک پیامک جدید آزمایش کنید؛ Retry پیام قدیمی ممکن است بدنه قبلی را بفرستد.",
            f"4️⃣ موتور تشخیص از <b>{len(BANK_PROFILES)}</b> بانک و برند بانکی پشتیبانی می‌کند.",
        ],
        subtitle="تأیید خودکار واریز با پیامک واقعی بانک",
        footer="این URL نقش کلید امنیتی دارد؛ آن را منتشر یا برای شخص دیگری ارسال نکنید.",
    )
    await callback.message.edit_text(text, reply_markup=sms_webhook_menu(docs_url))
    await callback.answer()



@router.callback_query(F.data == "sms:webhook:rotate")
async def sms_webhook_rotate_start(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
    await callback.message.edit_text(
        warning(
            "تعویض توکن امنیتی وبهوک پیامک",
            "با انجام این عملیات، نشانی فعلی وبهوک بلافاصله نامعتبر می‌شود و باید URL جدید را در SMS Forwarder جایگزین کنید.",
            footer="این عملیات روی API Keyها و Secret امضای Callback اثری ندارد.",
        ),
        reply_markup=sms_webhook_rotate_confirm_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "sms:webhook:rotate:confirm")
async def sms_webhook_rotate_confirm(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        rotate_merchant_sms_token(merchant)
        await session.commit()
        new_url = merchant_sms_webhook_url(merchant)
        docs_url = merchant_docs_url(merchant)
    await callback.message.edit_text(
        success(
            "توکن وبهوک پیامک تعویض شد",
            "\n".join([
                "نشانی قبلی دیگر معتبر نیست.",
                "",
                "<b>نشانی محرمانه جدید</b>",
                f"<code>{esc(new_url)}</code>",
            ]),
            footer="URL جدید را فقط در برنامه SMS Forwarder ثبت کنید و از انتشار آن خودداری کنید.",
        ),
        reply_markup=sms_webhook_menu(docs_url),
    )
    await callback.answer("توکن جدید فعال شد.")

@router.callback_query(F.data == "callback:panel")
async def callback_panel(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)
    docs_url = merchant_docs_url(merchant)
    status = badge("configured" if merchant.callback_url else "missing")
    text = panel(
        "🔔",
        "Callback نتیجه پرداخت",
        [
            f"📡 وضعیت اتصال: <b>{status}</b>",
            f"🌐 نشانی مقصد: <code>{esc(merchant.callback_url or 'تنظیم نشده')}</code>",
            f"🔐 Secret امضا: <code>{esc(merchant.callback_secret or 'ایجاد نشده')}</code>",
            "",
            "<b>هدرهای ارسال‌شده</b>",
            "<code>X-Gateway-Signature</code>",
            "<code>X-Gateway-Event</code>",
            "<code>X-Gateway-Delivery</code>",
            "<code>X-Gateway-Timestamp</code>",
        ],
        subtitle="اعلام آنی پرداخت موفق به سایت یا ربات شما",
        footer="سرور مقصد باید HTTPS عمومی داشته باشد و حداکثر ظرف ۱۰ ثانیه پاسخ 2xx برگرداند.",
    )
    await callback.message.edit_text(text, reply_markup=callback_menu(docs_url, bool(merchant.callback_url)))
    await callback.answer()

@router.callback_query(F.data == "callback:set")
async def callback_set_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CallbackConfigState.url)
    await callback.message.edit_text(
        panel(
            "🔔",
            "ثبت نشانی Callback",
            [
                "نشانی HTTPS عمومی سرور دریافت نتیجه را ارسال کنید.",
                "نمونه:",
                "<code>https://example.com/api/bluepay/webhook</code>",
            ],
            subtitle="اعلام پرداخت موفق به سایت یا ربات پذیرنده",
            footer="نشانی‌های localhost، IP خصوصی و HTTP ناامن پذیرفته نمی‌شوند.",
        ),
        reply_markup=flow_cancel_menu(),
    )
    await callback.answer()

@router.message(CallbackConfigState.url)
async def callback_set_value(message: Message, state: FSMContext):
    valid, result = validate_callback_url(message.text or "")
    if not valid:
        return await message.answer(
            warning("نشانی Callback معتبر نیست", esc(result), footer="یک نشانی HTTPS عمومی وارد کنید."),
            reply_markup=flow_cancel_menu(),
        )
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not merchant:
            await state.clear()
            return await message.answer(error("حساب پذیرنده یافت نشد", "دستور /start را ارسال و دوباره تلاش کنید."))
        merchant.callback_url = result
        if not merchant.callback_secret:
            merchant.callback_secret = random_secret(32)
        await session.commit()
    await state.clear()
    await message.answer(
        success(
            "Callback با موفقیت ثبت شد",
            f"🌐 نشانی مقصد:\n<code>{esc(result)}</code>",
            footer="برای اطمینان از صحت اتصال، از مرکز اتصال یک رویداد آزمایشی ارسال کنید.",
        ),
        reply_markup=main_menu(merchant.is_admin),
    )

@router.callback_query(F.data == "callback:remove")
async def callback_remove(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        merchant.callback_url = None
        await session.commit()
    await callback.message.edit_text(
        success(
            "نشانی Callback حذف شد",
            "تا زمان ثبت نشانی جدید، نتیجه پرداخت را از طریق API استعلام کنید.",
        ),
        reply_markup=connection_menu(merchant_docs_url(merchant), merchant_portal_url(merchant)),
    )
    await callback.answer("Callback حذف شد.")

@router.callback_query(F.data == "callback:secret")
async def callback_secret_regen(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        merchant.callback_secret = random_secret(32)
        await session.commit()
        secret = merchant.callback_secret
    await callback.message.answer(
        warning(
            "Secret امضا بازنشانی شد",
            f"Secret جدید:\n<code>{esc(secret)}</code>",
            footer="Secret قبلی دیگر معتبر نیست. همچنین نشانی وبهوک پیامک و مستندات اختصاصی تغییر کرده‌اند؛ نشانی‌های جدید را از مرکز اتصال دریافت کنید.",
        )
    )
    await callback.answer("Secret جدید ایجاد شد.")

@router.callback_query(F.data == "callback:test")
async def callback_test(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant or not merchant.callback_url:
        return await callback.answer("ابتدا نشانی Callback را ثبت کنید.", show_alert=True)
    await callback.answer("در حال آزمایش اتصال…")
    ok, result = await send_test_callback(merchant)
    if ok:
        await callback.message.answer(
            success(
                "آزمایش Callback موفق بود",
                f"سرور مقصد پاسخ معتبر برگرداند: <code>{esc(result)}</code>",
                footer="اتصال برای دریافت رویدادهای پرداخت آماده است.",
            )
        )
    else:
        await callback.message.answer(
            error(
                "آزمایش Callback ناموفق بود",
                f"نتیجه: <code>{esc(result)}</code>",
                footer="گواهی SSL، دسترس‌پذیری عمومی، زمان پاسخ و لاگ سرور مقصد را بررسی کنید.",
            )
        )

@router.message(Command("credit"))
async def admin_credit(message: Message):
    parts = (message.text or "").split()
    if len(parts) != 3:
        return await message.answer("فرمت: /credit TELEGRAM_ID AMOUNT_TOMAN")
    try:
        target_id = int(parts[1])
        amount_toman = int(parts[2])
        if target_id <= 0 or amount_toman <= 0:
            raise ValueError
    except ValueError:
        return await message.answer("شناسه کاربر یا مبلغ شارژ معتبر نیست.")
    async with SessionLocal() as session:
        admin = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not admin or not admin.is_admin:
            return await message.answer("این دستور فقط برای مدیر است.")
        target = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == target_id))
        if not target:
            return await message.answer("کاربر مقصد یافت نشد.")
        amount_rial = amount_toman * 10
        await credit_wallet(session, target, amount_rial, f"شارژ توسط مدیر {admin.telegram_user_id}", f"admin:{message.message_id}:{target.id}")
        await session.commit()
    await message.answer(f"✅ کیف پول کاربر {target_id} به مبلغ {amount_toman:,} تومان شارژ شد.")


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
            return await message.answer("ابتدا دستور /start را ارسال کنید.")
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
            return await message.answer("ابتدا دستور /start را ارسال کنید.")
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
        if fee_rial < 0:
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
    fee_label = "رایگان" if int(parts[2]) == 0 else f"{int(parts[2]):,} تومان"
    await message.answer(f"✅ کارمزد کاربر {target_id} روی {fee_label} تنظیم شد.")


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




@router.message(Command("teamaccess"))
async def team_access_command(message: Message):
    async with SessionLocal() as session:
        rows = list((await session.scalars(select(MerchantTeamMember).where(MerchantTeamMember.telegram_user_id == message.from_user.id, MerchantTeamMember.is_active.is_(True)).order_by(MerchantTeamMember.id.asc()))).all())
        merchants = {row.merchant_id: await session.get(Merchant, row.merchant_id) for row in rows}
    if not rows:
        return await message.answer(warning("دسترسی تیمی پیدا نشد", "مالک پذیرنده باید ابتدا شما را با /teamadd ثبت کند."))
    keyboard = []
    lines = []
    for row in rows:
        merchant = merchants.get(row.merchant_id)
        if not merchant or not merchant.is_active:
            continue
        lines.append(f"• {esc(merchant.name)} · {esc(role_label(row.role))}")
        keyboard.append([InlineKeyboardButton(text=f"{merchant.name} · {role_label(row.role)}", url=team_portal_url(row))])
    await message.answer(panel("👥", "دسترسی‌های تیمی من", lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@router.callback_query(F.data == "team:panel")
async def team_panel(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده پیدا نشد.", show_alert=True)
        rows = await list_team_members(session, merchant.id)
    lines = [
        f"مالک اصلی: <code>{merchant.telegram_user_id}</code> · دسترسی کامل",
        "",
        "افزودن عضو:",
        "<code>/teamadd TELEGRAM_ID ROLE</code>",
        "نقش‌ها: <code>finance</code>، <code>developer</code>، <code>support</code>، <code>viewer</code>",
    ]
    keyboard = []
    for row in rows:
        lines.append(f"• <code>{row.telegram_user_id}</code> · {esc(role_label(row.role))} · {'فعال' if row.is_active else 'غیرفعال'}")
        if row.is_active:
            keyboard.append([InlineKeyboardButton(text=f"قطع دسترسی {row.telegram_user_id}", callback_data=f"team:remove:{row.telegram_user_id}")])
    keyboard.append([InlineKeyboardButton(text="‹ بازگشت به اتصال", callback_data="connect")])
    await callback.message.edit_text(panel("👥", "اعضای تیم پذیرنده", lines, footer="هر عضو فقط مجوزهای نقش خود را دریافت می‌کند."), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@router.message(Command("teamadd"))
async def team_add_command(message: Message):
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit():
        return await message.answer("فرمت: <code>/teamadd TELEGRAM_ID finance|developer|support|viewer</code>")
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not merchant:
            return await message.answer("حساب پذیرنده پیدا نشد.")
        try:
            row = await add_team_member(session, merchant, int(parts[1]), parts[2], invited_by=message.from_user.id)
        except ValueError as exc:
            return await message.answer(error("عضو اضافه نشد", esc(str(exc))))
        await session.commit()
    await message.answer(success("عضو تیم ثبت شد", f"کاربر <code>{row.telegram_user_id}</code> با نقش <b>{esc(role_label(row.role))}</b> فعال شد."))


@router.message(Command("team"))
async def team_list_command(message: Message):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not merchant:
            return await message.answer("حساب پذیرنده پیدا نشد.")
        rows = await list_team_members(session, merchant.id)
    text = [f"مالک: <code>{merchant.telegram_user_id}</code>"]
    text.extend(f"<code>{row.telegram_user_id}</code> · {esc(role_label(row.role))} · {'فعال' if row.is_active else 'غیرفعال'}" for row in rows)
    await message.answer(panel("👥", "فهرست اعضای تیم", text or ["عضوی ثبت نشده است."]))


@router.message(Command("teamremove"))
async def team_remove_command(message: Message):
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("فرمت: <code>/teamremove TELEGRAM_ID</code>")
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not merchant:
            return await message.answer("حساب پذیرنده پیدا نشد.")
        ok = await remove_team_member(session, merchant.id, int(parts[1]))
        await session.commit()
    await message.answer(success("دسترسی قطع شد", "عضو غیرفعال شد.") if ok else warning("عضو پیدا نشد", "شناسه در تیم شما ثبت نشده است."))


@router.callback_query(F.data.startswith("team:remove:"))
async def team_remove_callback(callback: CallbackQuery):
    user_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پیدا نشد.", show_alert=True)
        ok = await remove_team_member(session, merchant.id, user_id)
        await session.commit()
    await callback.answer("دسترسی قطع شد." if ok else "عضو پیدا نشد.", show_alert=True)
    await team_panel(callback)


@router.callback_query(F.data == "sms:devices")
async def sms_devices_panel(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پیدا نشد.", show_alert=True)
        rows = await list_sms_devices(session, merchant.id)
    lines = [
        "ثبت امن دستگاه:",
        "<code>/smsdeviceadd phone-1</code>",
        "Secret فقط یک‌بار نمایش داده می‌شود و برای امضای HMAC بدنه استفاده می‌شود.",
        "",
    ]
    keyboard = []
    for row in rows:
        lines.append(f"• <b>{esc(row.name)}</b> · <code>{esc(row.device_id)}</code> · {'فعال' if row.is_active else 'متوقف'} · درخواست {row.request_count}")
        if row.is_active:
            keyboard.append([InlineKeyboardButton(text=f"تعویض Secret {row.device_id}", callback_data=f"sms:device:rotate:{row.id}")])
    keyboard.append([InlineKeyboardButton(text="‹ بازگشت به اتصال", callback_data="connect")])
    await callback.message.edit_text(panel("📱", "دستگاه‌های امن پیامک", lines, footer="پس از ثبت اولین دستگاه امن، درخواست دستگاه‌های ناشناس رد می‌شود."), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


@router.message(Command("smsdeviceadd"))
async def sms_device_add_command(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        return await message.answer("فرمت: <code>/smsdeviceadd phone-1</code>")
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not merchant:
            return await message.answer("حساب پذیرنده پیدا نشد.")
        try:
            row, secret = await register_sms_device(session, merchant, parts[1], name=parts[1], require_hmac=True)
        except ValueError as exc:
            return await message.answer(error("ثبت دستگاه ناموفق بود", esc(str(exc))))
        await session.commit()
    await message.answer(
        success(
            "دستگاه امن ثبت شد",
            "\n".join([
                f"Device ID: <code>{esc(row.device_id)}</code>",
                f"Device Secret: <code>{esc(secret)}</code>",
                "هدر زمان: <code>X-BluePay-Timestamp</code>",
                "هدر امضا: <code>X-BluePay-Signature</code>",
                "فرمول: <code>sha256=HMAC_SHA256(secret, timestamp + '.' + raw_body)</code>",
            ]),
            footer="Secret را همین حالا ذخیره کنید؛ بعداً فقط با Rotation قابل دریافت است.",
        )
    )


@router.callback_query(F.data.startswith("sms:device:rotate:"))
async def sms_device_rotate_callback(callback: CallbackQuery):
    device_db_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        row = await session.scalar(select(SmsDevice).where(SmsDevice.id == device_db_id, SmsDevice.merchant_id == merchant.id if merchant else -1))
        if not merchant or not row:
            return await callback.answer("دستگاه پیدا نشد.", show_alert=True)
        secret = await rotate_sms_device_secret(session, merchant, row)
        await session.commit()
    await callback.message.answer(success("Secret دستگاه تعویض شد", f"Device: <code>{esc(row.device_id)}</code>\nSecret جدید: <code>{esc(secret)}</code>", footer="Secret قبلی از همین لحظه نامعتبر است."))
    await callback.answer()


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
        "📦 فایل ZIP نسخه جدید را در همین گفتگو ارسال کنید.\n"
        "مخزن و شاخه به‌صورت خودکار از Deploy متصل Railway تشخیص داده می‌شوند؛ تنظیم دیگری لازم نیست."
    )
    await callback.answer()


@router.message(F.document)
async def release_upload(message: Message, state: FSMContext):
    async with SessionLocal() as session:
        admin = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not admin or not admin.is_admin:
            return
        repository = settings.github_repository
        branch = settings.github_branch
    file_name = (message.document.file_name or "").strip()
    if not file_name.lower().endswith(".zip"):
        return await message.answer(warning("فرمت فایل پشتیبانی نمی‌شود", "فقط بسته ZIP نسخه جدید پذیرفته می‌شود."))
    if message.document.file_size and message.document.file_size > 25 * 1024 * 1024:
        return await message.answer(
            warning("حجم فایل بیش از حد مجاز است", "حداکثر حجم بسته انتشار ۲۵ مگابایت است.")
        )

    data = await state.get_data()
    current_state = await state.get_state()
    target = data.get("release_target") if current_state == AdminReleaseState.waiting_zip.state else "production"
    target = target if target in {"staging", "production"} else "production"
    target_branch = settings.release_staging_branch if target == "staging" else branch

    status = await message.answer(info("در حال بررسی بسته", f"هدف: <b>{'Staging' if target == 'staging' else 'Production'}</b>\nساختار، Syntax، Migration و دسترسی GitHub در حال بررسی است."))
    buffer = io.BytesIO()
    package = None
    try:
        await message.bot.download(message.document, destination=buffer)
        package = validate_release_zip(buffer.getvalue())
        publisher = GitHubPublisher(settings.github_token or "", repository, branch or "main")
        preflight = await publisher.preflight(needs_workflows=bool(package.validation.get("workflow_files")))
        await status.edit_text(
            info(
                "بسته معتبر و Preflight موفق است",
                "\n".join([
                    f"نسخه: <code>{esc(package.version)}</code>",
                    f"SHA-256: <code>{package.sha256[:16]}…</code>",
                    f"فایل‌ها: <b>{package.validation['file_count']}</b>",
                    f"Python Compile: <b>{package.validation['python_files_compiled']}</b>",
                    f"شاخه مقصد: <code>{esc(target_branch)}</code>",
                    f"HEAD فعلی: <code>{esc(preflight['head'][:12])}</code>",
                ]),
            )
        )
        result = await publisher.publish(package, branch=target_branch)
        update_status = "staged" if target == "staging" else "published"
        async with SessionLocal() as session:
            session.add(
                UpdateLog(
                    version=package.version,
                    commit_sha=result.commit_sha,
                    previous_commit_sha=result.previous_commit_sha,
                    package_sha256=package.sha256,
                    status=update_status,
                    message=package.description,
                    validation_json=json.dumps(package.validation, ensure_ascii=False),
                    telegram_user_id=message.from_user.id,
                )
            )
            await session.commit()
        await state.clear()
        footer = (
            "شاخه آزمایشی آماده است؛ محیط Staging Railway را به این شاخه متصل کنید."
            if target == "staging"
            else "Railway Deploy را آغاز می‌کند؛ نسخه فقط پس از موفق‌شدن /ready فعال می‌شود."
        )
        await status.edit_text(
            success(
                "نسخه منتشر شد",
                "\n".join([
                    f"📦 نسخه: <code>{esc(package.version)}</code>",
                    f"🌿 شاخه: <code>{esc(result.branch)}</code>",
                    f"🔖 Commit: <code>{result.commit_sha[:12]}</code>",
                    f"↩️ نقطه Rollback: <code>{result.previous_commit_sha[:12]}</code>",
                    f"📁 فایل‌های منتشرشده: <b>{result.changed_files}</b>",
                ]),
                footer=footer,
            )
        )
    except Exception as exc:
        await state.clear()
        try:
            async with SessionLocal() as session:
                session.add(UpdateLog(version=package.version if package else "unknown", package_sha256=package.sha256 if package else None, status="failed", message=str(exc)[:2000], validation_json=json.dumps(package.validation, ensure_ascii=False) if package else None, telegram_user_id=message.from_user.id))
                await session.commit()
        except Exception:
            pass
        await status.edit_text(error("انتشار نسخه ناموفق بود", f"<code>{esc(str(exc))}</code>"))
