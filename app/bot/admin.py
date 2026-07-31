from __future__ import annotations

import html
import math
import secrets
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.bot.keyboards import admin_menu, main_menu
from app.bot.states import AdminFeeState, AdminSmsApproveState, AdminWalletAdjustState
from app.bot.presentation import fee_mode_label, invoice_status_label, sms_result_label
from app.core.config import settings
from app.db.session import SessionLocal
from app.models import BankCard, Invoice, Merchant, SmsTransaction, UpdateLog, WalletLedger
from app.services.callback_service import send_paid_callback
from app.services.integration_service import merchant_docs_url, merchant_sms_webhook_url
from app.services.invoice_service import confirm_invoice_paid, release_invoice_reservation
from app.services.storage_service import storage
from app.version import APP_VERSION

router = Router(name="admin")
PAGE_SIZE = 8


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
        [InlineKeyboardButton(text="⚙️ تعیین کارمزد", callback_data=f"admin:mfee:{merchant.id}:{page}")],
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


def invoices_keyboard(invoices: list[Invoice], back_data: str = "admin:panel") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"#{item.id} • {invoice_status_label(item.status)} • {toman(item.payable_amount_rial)} ت", callback_data=f"admin:invoice:{item.id}")]
        for item in invoices
    ]
    rows.append([InlineKeyboardButton(text="↩️ بازگشت", callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("admin"))
async def admin_command(message: Message):
    if not await require_admin_message(message):
        return
    await message.answer("👑 <b>مدیریت سامانه BluePay</b>\n━━━━━━━━━━━━━━━━\nلطفاً یکی از بخش‌های زیر را انتخاب کنید.", reply_markup=admin_menu())


@router.callback_query(F.data == "admin:panel")
async def admin_panel(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.message.edit_text(
        "👑 <b>مدیریت سامانه BluePay</b>\n━━━━━━━━━━━━━━━━\nمدیریت پذیرندگان، کیف پول‌ها، فاکتورها، پیامک‌های بانکی و وضعیت سامانه از این بخش انجام می‌شود.",
        reply_markup=admin_menu(),
    )
    await callback.answer()


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
    text = (
        "📊 <b>داشبورد مدیریتی</b>\n━━━━━━━━━━━━━━━━\n"
        f"👥 پذیرندگان: <b>{merchants:,}</b> (فعال: {active_merchants:,})\n"
        f"🏦 کارت‌های ثبت‌شده: <b>{cards:,}</b>\n"
        f"🧾 کل فاکتورها: <b>{invoices:,}</b>\n"
        f"⏳ در انتظار: <b>{pending:,}</b>\n"
        f"✅ پرداخت‌شده: <b>{paid:,}</b>\n"
        f"🔎 نیازمند بررسی: <b>{review:,}</b>\n\n"
        f"💰 مجموع کیف پول‌ها: <b>{toman(wallet_total)} تومان</b>\n"
        f"🔒 مجموع رزروشده: <b>{toman(reserved_total)} تومان</b>\n"
        f"💳 مجموع مبالغ تأییدشده: <b>{toman(paid_total)} تومان</b>"
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
                text=f"{'🟢' if item.is_active else '🔴'} {short(item.name, 18)} • {item.telegram_user_id}",
                callback_data=f"admin:merchant:{item.id}:{page}",
            )
        ]
        for item in merchants
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"admin:merchants:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"admin:merchants:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="👑 منوی مدیریت", callback_data="admin:panel")])
    await callback.message.edit_text(
        f"👥 <b>پذیرندگان</b>\n\nصفحه {page + 1} از {pages} • مجموع {total:,}",
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
            return await callback.answer("پذیرنده پیدا نشد.", show_alert=True)
        cards_count = await session.scalar(select(func.count(BankCard.id)).where(BankCard.merchant_id == merchant.id)) or 0
        invoice_count = await session.scalar(select(func.count(Invoice.id)).where(Invoice.merchant_id == merchant.id)) or 0
        paid_count = await session.scalar(
            select(func.count(Invoice.id)).where(Invoice.merchant_id == merchant.id, Invoice.status == "paid")
        ) or 0
    text = (
        "👤 <b>مشخصات پذیرنده</b>\n━━━━━━━━━━━━━━━━\n"
        f"شناسه داخلی: <code>{merchant.id}</code>\n"
        f"Telegram ID: <code>{merchant.telegram_user_id}</code>\n"
        f"نام: {html.escape(merchant.name)}\n"
        f"وضعیت: {'🟢 فعال' if merchant.is_active else '🔴 غیرفعال'}\n"
        f"سطح: {'👑 مدیر' if merchant.is_admin else 'پذیرنده'}\n\n"
        f"موجودی: <b>{toman(merchant.wallet_balance_rial)} تومان</b>\n"
        f"رزروشده: {toman(merchant.reserved_balance_rial)} تومان\n"
        f"قابل استفاده: {toman(merchant.available_balance_rial)} تومان\n"
        f"کارمزد هر تأیید: {toman(merchant.verification_fee_rial)} تومان\n"
        f"مدل کارمزد: <b>{html.escape(fee_mode_label(merchant.fee_mode))}</b>\n"
        f"API: <code>{html.escape(merchant.api_key_prefix or 'ساخته نشده')}</code>\n"
        f"Callback: <code>{html.escape(short(merchant.callback_url, 42))}</code>\n\n"
        f"وبهوک پیامک:\n<code>{html.escape(merchant_sms_webhook_url(merchant))}</code>\n\n"
        f"مستندات اختصاصی:\n<code>{html.escape(merchant_docs_url(merchant))}</code>\n\n"
        f"کارت‌ها: {cards_count:,} • فاکتورها: {invoice_count:,} • پرداخت‌شده: {paid_count:,}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=merchant_detail_keyboard(merchant, page, callback.from_user.id),
    )
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


@router.callback_query(F.data.startswith("admin:mfee:"))
async def admin_fee_start(callback: CallbackQuery, state: FSMContext):
    if not await require_admin_callback(callback):
        return
    _, _, merchant_id, page = callback.data.split(":")
    await state.set_state(AdminFeeState.amount)
    await state.update_data(merchant_id=int(merchant_id), page=int(page))
    await callback.message.answer("مبلغ کارمزد هر تأیید را به تومان و فقط به‌صورت عدد وارد کنید:")
    await callback.answer()


@router.message(AdminFeeState.amount)
async def admin_fee_amount(message: Message, state: FSMContext):
    if not await require_admin_message(message):
        await state.clear()
        return
    raw = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if not raw or int(raw) <= 0:
        return await message.answer("مبلغ کارمزد باید بیشتر از صفر باشد.")
    data = await state.get_data()
    async with SessionLocal() as session:
        merchant = await session.get(Merchant, data["merchant_id"])
        if not merchant:
            await state.clear()
            return await message.answer("پذیرنده پیدا نشد.")
        merchant.verification_fee_rial = int(raw) * 10
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ کارمزد روی <b>{int(raw):,} تومان</b> تنظیم شد.",
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
    text = "🧾 <b>آخرین فاکتورها</b>\n\nبرای مشاهده جزئیات، یکی از فاکتورها را انتخاب کنید."
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
            return await callback.answer("فاکتور پیدا نشد.", show_alert=True)
        merchant = await session.get(Merchant, invoice.merchant_id)
        card = await session.get(BankCard, invoice.card_id)
    text = (
        "🧾 <b>جزئیات فاکتور</b>\n━━━━━━━━━━━━━━━━\n"
        f"شناسه داخلی: <code>{invoice.id}</code>\n"
        f"Payment ID: <code>{invoice.token}</code>\n"
        f"Order ID: <code>{html.escape(invoice.order_id)}</code>\n"
        f"پذیرنده: {html.escape(merchant.name if merchant else '-')} (<code>{merchant.telegram_user_id if merchant else '-'}</code>)\n"
        f"وضعیت: <b>{html.escape(invoice_status_label(invoice.status))}</b>\n"
        f"مبلغ اصلی: {toman(invoice.base_amount_rial)} تومان\n"
        f"کارمزد: {toman(invoice.fee_amount_rial)} تومان\n"
        f"کد تطبیق مبلغ: +{toman(invoice.unique_amount_rial)} تومان\n"
        f"پرداختی مشتری: <b>{toman(invoice.payable_amount_rial)} تومان</b>\n"
        f"کارت مقصد: {html.escape(card.bank_code if card else '-')} • ****{card.card_last4 if card else '-'}\n"
        f"مرجع: <code>{html.escape(invoice.reference_number or '-')}</code>"
    )
    rows = []
    if invoice.status == "pending":
        rows.append([InlineKeyboardButton(text="❌ لغو فاکتور", callback_data=f"admin:invoicecancel:{invoice.id}")])
    rows.extend(
        [
            [InlineKeyboardButton(text="🧾 فهرست فاکتورها", callback_data="admin:invoices")],
            [InlineKeyboardButton(text="👑 منوی مدیریت", callback_data="admin:panel")],
        ]
    )
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
        [
            InlineKeyboardButton(
                text=f"SMS #{row.id} • {row.bank_code} • {toman(row.amount_rial)} ت",
                callback_data=f"admin:sms:{row.id}",
            )
        ]
        for row in rows
    ]
    keyboard_rows.append([InlineKeyboardButton(text="👑 منوی مدیریت", callback_data="admin:panel")])
    text = "🔎 <b>پیامک‌های نیازمند بررسی</b>\n\n" + (
        "یکی از پیامک‌ها را برای بررسی انتخاب کنید." if rows else "موردی برای بررسی وجود ندارد."
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
        return await callback.answer("پیامک پیدا نشد.", show_alert=True)
    text = (
        "📨 <b>جزئیات پیامک بانکی</b>\n━━━━━━━━━━━━━━━━\n"
        f"شناسه: <code>{sms.id}</code>\n"
        f"بانک: {html.escape(sms.bank_code)}\n"
        f"مبلغ: <b>{toman(sms.amount_rial)} تومان</b>\n"
        f"کارت: ****{sms.card_last4 or '-'}\n"
        f"مرجع: <code>{html.escape(sms.reference_number or '-')}</code>\n"
        f"وضعیت: <code>{sms.status}</code>\n"
        f"اطمینان تشخیص: {sms.parse_confidence}%\n"
        f"نتیجه بررسی: {html.escape(sms_result_label(sms.status))}\n\n"
        f"<code>{html.escape(sms.raw_message[:700])}</code>"
    )
    rows = [
        [InlineKeyboardButton(text="✅ اتصال به فاکتور", callback_data=f"admin:sms:approve:{sms.id}")],
        [InlineKeyboardButton(text="❌ رد پیامک", callback_data=f"admin:sms:reject:{sms.id}")],
        [InlineKeyboardButton(text="↩️ فهرست بررسی", callback_data="admin:reviews")],
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
    error = html.escape(str(backup.get("last_error") or "ندارد"))
    text = (
        "🖥 <b>وضعیت سامانه</b>\n━━━━━━━━━━━━━━━━\n"
        f"نسخه برنامه: <code>{APP_VERSION}</code>\n"
        f"دامنه: <code>{html.escape(settings.base_url)}</code>\n"
        f"مخزن: <code>{html.escape(settings.github_repository)}</code>\n"
        f"شاخه انتشار: <code>{html.escape(settings.github_branch)}</code>\n"
        f"شاخه دیتابیس: <code>{html.escape(settings.data_branch)}</code>\n\n"
        f"Backup در صف: {'بله' if backup.get('dirty') else 'خیر'}\n"
        f"آخرین Backup: <code>{html.escape(str(backup.get('last_backup_at') or '-'))}</code>\n"
        f"آخرین Restore: <code>{html.escape(str(backup.get('last_restore_at') or '-'))}</code>\n"
        f"آخرین خطای ذخیره‌سازی: <code>{error[:500]}</code>\n\n"
        f"آخرین نسخه منتشرشده: <code>{html.escape(last_update.version if last_update else '-')}</code>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💾 پشتیبان‌گیری فوری", callback_data="admin:backup")],
            [InlineKeyboardButton(text="📦 آپدیت سیستم", callback_data="admin:update")],
            [InlineKeyboardButton(text="👑 منوی مدیریت", callback_data="admin:panel")],
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
        "📦 فایل ZIP نسخه جدید را در همین گفتگو ارسال کنید.\n\n"
        "پس از اعتبارسنجی بسته، تغییرات در GitHub ثبت می‌شود و Railway انتشار نسخه جدید را به‌صورت خودکار آغاز می‌کند."
    )
    await callback.answer()
