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
)
from app.bot.states import AddCardState, CallbackConfigState, ManualInvoiceState
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
    text = panel(
        "💰",
        "کیف پول کارمزد",
        [
            f"💵 موجودی کل: <b>{money_toman(merchant.wallet_balance_rial)}</b>",
            f"🔒 اعتبار رزروشده: <b>{money_toman(merchant.reserved_balance_rial)}</b>",
            f"✅ اعتبار قابل استفاده: <b>{money_toman(merchant.available_balance_rial)}</b>",
            f"🧾 هزینه هر تأیید موفق: <b>{money_toman(merchant.verification_fee_rial)}</b>",
            "",
            f"📊 نسبت اعتبار رزروشده: <b>{reserve_percent}%</b>",
        ],
        subtitle="کنترل اعتبار موردنیاز برای تأیید پرداخت‌ها",
        footer="هزینه فقط پس از تأیید قطعی پرداخت کسر می‌شود؛ فاکتور منقضی یا لغوشده هزینه‌ای ندارد.",
    )
    await callback.message.edit_text(text, reply_markup=main_menu(merchant.is_admin))
    await callback.answer()

@router.callback_query(F.data == "fee")
async def fee(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)

    text = panel(
        "⚙️",
        "سیاست پرداخت کارمزد",
        [
            f"تنظیم فعلی: <b>{fee_title(merchant.fee_mode)}</b>",
            "",
            "👤 <b>مشتری</b> — کل هزینه تأیید به مبلغ فاکتور افزوده می‌شود.",
            "🤝 <b>تقسیم مساوی</b> — نیمی از هزینه به مشتری و نیم دیگر به پذیرنده تعلق می‌گیرد.",
            "🏪 <b>پذیرنده</b> — مبلغ سفارش برای مشتری بدون افزایش باقی می‌ماند.",
        ],
        subtitle="نحوه تقسیم هزینه تأیید هر تراکنش",
        footer="این انتخاب، پیش‌فرض فاکتورهای بعدی است و هنگام ساخت هر فاکتور قابل تغییر خواهد بود.",
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
    code = normalize_bank_code(message.text)
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
    await state.update_data(holder=message.text.strip())
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
    source_id = None if message.text.strip() == "-" else message.text.strip()
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
    if not raw or int(raw) < 1000:
        return await message.answer(
            warning("مبلغ معتبر نیست", "مبلغ سفارش باید عددی و حداقل ۱٬۰۰۰ تومان باشد."),
            reply_markup=flow_cancel_menu(),
        )

    amount = int(raw)
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
    description = message.text.strip()[:500]
    if not description:
        return await message.answer("عنوان فاکتور الزامی است.", reply_markup=flow_cancel_menu())

    await state.update_data(description=description)
    await state.set_state(ManualInvoiceState.fee_mode)
    merchant = await current_merchant(message.from_user.id)
    if not merchant:
        await state.clear()
        return await message.answer("حساب پذیرنده یافت نشد. لطفاً دستور /start را ارسال کنید.")

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
    mode = message.text.strip().lower()
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
            f"🧾 هزینه تأیید: <b>{money_toman(fee_rial)}</b>",
            f"👤 سهم مشتری از هزینه: <b>{money_toman(customer_fee_rial)}</b>",
            f"🔢 کد تطبیق یکتا: <b>۱ تا ۹۹۹ تومان</b>",
            f"💳 مبلغ پیش از کد تطبیق: <b>{money_toman(nominal_payable_rial)}</b>",
            "",
            f"🏦 کارت مقصد: <b>{bank_title(card.bank_code)} •••• {card.card_last4}</b>",
            f"⚙️ سیاست کارمزد: <b>{fee_title(mode)}</b>",
            f"⏱ اعتبار لینک: <b>{settings.invoice_ttl_minutes} دقیقه</b>",
        ],
        subtitle="آخرین مرحله پیش از صدور لینک پرداخت",
        footer="پس از صدور، هزینه تأیید در کیف پول رزرو می‌شود و در صورت لغو یا انقضا آزاد خواهد شد.",
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
        text = success(
            "فاکتور آماده دریافت وجه است",
            "\n".join([
                f"📝 عنوان: <b>{esc(invoice.description or '-')}</b>",
                f"🧾 شناسه پرداخت: <code>{invoice.token}</code>",
                f"💳 مبلغ دقیق واریز: <b>{money_toman(invoice.payable_amount_rial)}</b>",
                f"🔢 کد تطبیق: <b>+{money_toman(invoice.unique_amount_rial)}</b>",
                f"🏦 مقصد: <b>{bank_title(card.bank_code)} •••• {card.card_last4}</b>",
                f"⏳ وضعیت: <b>{badge('pending')}</b>",
                "",
                f"🔗 لینک پرداخت:\n{payment_url}",
            ]),
            footer="لینک را برای مشتری ارسال کنید. وضعیت پرداخت پس از دریافت پیامک بانک به‌صورت خودکار به‌روزرسانی می‌شود.",
        )
        await callback.message.edit_text(text, reply_markup=payment_created_menu(payment_url))
        await callback.answer("فاکتور صادر شد.")
    except Exception as exc:
        await state.set_state(ManualInvoiceState.confirm)
        await callback.answer("صدور فاکتور ناموفق بود.", show_alert=True)
        await callback.message.edit_text(
            error(
                "فاکتور صادر نشد",
                f"کد خطا: <code>{esc(str(exc))}</code>",
                footer="اطلاعات این فاکتور حفظ شده است؛ پس از رفع مشکل دوباره تأیید کنید.",
            ),
            reply_markup=invoice_confirm_menu(),
        )

@router.callback_query(F.data == "connect")
async def connection_panel(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)
    docs_url = merchant_docs_url(merchant)
    callback_status = badge("configured" if merchant.callback_url else "missing")
    api_status = badge("configured" if merchant.api_key_prefix else "missing")
    ready_count = int(bool(merchant.api_key_prefix)) + int(bool(merchant.callback_url)) + 1
    text = panel(
        "🔌",
        "مرکز اتصال و توسعه‌دهندگان",
        [
            f"📈 پیشرفت راه‌اندازی: <b>{ready_count} از 3 سرویس</b>",
            "",
            f"🔑 دسترسی API: <b>{api_status}</b>",
            f"📲 دریافت پیامک بانکی: <b>{badge('ready')}</b>",
            f"🔔 Callback نتیجه پرداخت: <b>{callback_status}</b>",
            "",
            "<b>مسیر پیشنهادی اتصال</b>",
            "1️⃣ کلید API را ایجاد و در سرور خود ذخیره کنید.",
            "2️⃣ وبهوک اختصاصی پیامک را در گوشی متصل کنید.",
            "3️⃣ آدرس Callback سایت یا ربات را ثبت و آزمایش کنید.",
            "4️⃣ از مستندات اختصاصی یک فاکتور آزمایشی بسازید.",
            "",
            f"📘 مستندات حساب:\n<code>{esc(docs_url)}</code>",
        ],
        subtitle="اتصال امن سایت، ربات و SMS Forwarder",
        footer="تمام نشانی‌ها و کلیدهای این بخش اختصاصی حساب شما هستند.",
    )
    await callback.message.edit_text(text, reply_markup=connection_menu(docs_url))
    await callback.answer()

@router.callback_query(F.data == "api")
async def api_panel(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا وارد حساب پذیرنده شوید.", show_alert=True)
    prefix = merchant.api_key_prefix or "ایجاد نشده"
    callback_url = merchant.callback_url or "تنظیم نشده"
    docs_url = merchant_docs_url(merchant)
    text = panel(
        "🔑",
        "دسترسی API",
        [
            f"📡 وضعیت: <b>{badge('configured' if merchant.api_key_prefix else 'missing')}</b>",
            f"🪪 پیشوند کلید: <code>{esc(prefix)}</code>",
            f"🌐 آدرس پایه: <code>{settings.base_url}/api/v1</code>",
            f"🔔 Callback پیش‌فرض: <code>{esc(callback_url)}</code>",
            "",
            "<b>هدر احراز هویت</b>",
            "<code>X-API-Key: gw_your_private_key</code>",
        ],
        subtitle="ساخت فاکتور و استعلام پرداخت از سایت یا ربات",
        footer="کلید کامل فقط هنگام ایجاد نمایش داده می‌شود. آن را در کد سمت کاربر یا مخزن عمومی قرار ندهید.",
    )
    await callback.message.edit_text(text, reply_markup=api_menu(docs_url))
    await callback.answer()

@router.callback_query(F.data == "api:regen")
async def api_regen(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        if not merchant:
            return await callback.answer("حساب پذیرنده یافت نشد.", show_alert=True)
        key = await regenerate_api_key(session, merchant)
        await session.commit()
    await callback.message.answer(
        success(
            "کلید API جدید صادر شد",
            "\n".join([
                "این کلید فقط همین یک‌بار نمایش داده می‌شود:",
                "",
                f"<code>{key}</code>",
                "",
                "هدر درخواست‌ها:",
                "<code>X-API-Key: YOUR_KEY</code>",
            ]),
            footer="کلید را فقط در Backend یا تنظیمات محرمانه ربات نگهداری کنید؛ ایجاد کلید جدید، کلید قبلی را باطل می‌کند.",
        )
    )
    await callback.answer("کلید جدید ایجاد شد.")

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
        footer="سرور مقصد باید HTTPS عمومی داشته باشد و حداکثر ظرف ۱۲ ثانیه پاسخ 2xx برگرداند.",
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
        reply_markup=connection_menu(merchant_docs_url(merchant)),
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
        "📦 فایل ZIP نسخه جدید را در همین گفتگو ارسال کنید.\n"
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
        return await message.answer(warning("فرمت فایل پشتیبانی نمی‌شود", "فقط بسته ZIP نسخه جدید پذیرفته می‌شود."))

    status = await message.answer(info("در حال بررسی بسته", "ساختار فایل و اطلاعات نسخه در حال اعتبارسنجی است."))
    buffer = io.BytesIO()
    await message.bot.download(message.document, destination=buffer)
    try:
        package = validate_release_zip(buffer.getvalue())
        await status.edit_text(info("بسته معتبر است", f"نسخه <code>{esc(package.version)}</code> در حال انتشار در GitHub است."))
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
            success(
                "نسخه در GitHub منتشر شد",
                "\n".join([
                    f"📦 نسخه: <code>{esc(package.version)}</code>",
                    f"🔖 Commit: <code>{commit_sha[:12]}</code>",
                    "🚄 Railway باید استقرار خودکار را آغاز کند.",
                ]),
                footer="تا موفق‌شدن Healthcheck، نسخه فعال قبلی به کار خود ادامه می‌دهد.",
            )
        )
    except Exception as exc:
        await status.edit_text(error("انتشار نسخه ناموفق بود", f"<code>{esc(str(exc))}</code>"))

