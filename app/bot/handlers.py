from __future__ import annotations

import html
import io
import secrets
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.keyboards import api_menu, cards_menu, fee_menu, main_menu
from app.bot.states import AddCardState, ManualInvoiceState
from app.core.config import settings
from app.core.security import encrypt_text
from app.db.session import SessionLocal
from app.models import BankCard, Invoice, Merchant, SmsTransaction, UpdateLog
from app.services.callback_service import send_paid_callback
from app.services.github_service import GitHubPublisher, validate_release_zip
from app.services.invoice_service import confirm_invoice_paid, create_invoice, release_invoice_reservation
from app.services.merchant_service import credit_wallet, get_or_create_merchant, regenerate_api_key
from app.services.settings_service import get_setting
from app.parsers import normalize_bank_code

router = Router()


def toman(value_rial: int) -> str:
    return f"{value_rial // 10:,}"


async def current_merchant(user_id: int) -> Merchant | None:
    async with SessionLocal() as session:
        return await session.scalar(select(Merchant).where(Merchant.telegram_user_id == user_id))


@router.message(CommandStart())
async def start(message: Message):
    async with SessionLocal() as session:
        merchant, created = await get_or_create_merchant(
            session,
            message.from_user.id,
            message.from_user.full_name,
        )
        await session.commit()
    extra = "\n\n👑 چون اولین کاربر سیستم هستی، مدیر اصلی شدی." if created and merchant.is_admin else ""
    await message.answer(
        "سلام! به پنل درگاه واسط خوش آمدی. از منوی زیر کارت بانکی ثبت کن، کیف پول را شارژ کن و فاکتور بساز."
        + extra,
        reply_markup=main_menu(merchant.is_admin),
    )


@router.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    await callback.message.edit_text("پنل اصلی درگاه:", reply_markup=main_menu(bool(merchant and merchant.is_admin)))
    await callback.answer()


@router.callback_query(F.data == "wallet")
async def wallet(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    if not merchant:
        return await callback.answer("ابتدا /start را بزن", show_alert=True)
    text = (
        "💰 <b>کیف پول کارمزد</b>\n\n"
        f"موجودی کل: <b>{toman(merchant.wallet_balance_rial)} تومان</b>\n"
        f"رزروشده: {toman(merchant.reserved_balance_rial)} تومان\n"
        f"قابل استفاده: {toman(merchant.available_balance_rial)} تومان\n"
        f"کارمزد هر تأیید: {toman(merchant.verification_fee_rial)} تومان"
    )
    await callback.message.edit_text(text, reply_markup=main_menu(merchant.is_admin))
    await callback.answer()


@router.callback_query(F.data == "fee")
async def fee(callback: CallbackQuery):
    await callback.message.edit_text("نحوه تقسیم کارمزد را انتخاب کن:", reply_markup=fee_menu())
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
    labels = {"customer": "کامل با مشتری", "split": "نصف‌نصف", "merchant": "کامل با پذیرنده"}
    await callback.message.edit_text(f"✅ تنظیم شد: {labels[mode]}", reply_markup=main_menu(merchant.is_admin))
    await callback.answer()


@router.callback_query(F.data == "cards")
async def cards(callback: CallbackQuery):
    await callback.message.edit_text("مدیریت کارت‌های بانکی:", reply_markup=cards_menu())
    await callback.answer()


@router.callback_query(F.data == "card:add")
async def add_card_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddCardState.bank)
    await callback.message.answer("نام یا کد بانک را بفرست؛ مثال: mellat یا ملت")
    await callback.answer()


@router.message(AddCardState.bank)
async def add_card_bank(message: Message, state: FSMContext):
    await state.update_data(bank=normalize_bank_code(message.text))
    await state.set_state(AddCardState.card_number)
    await message.answer("شماره ۱۶ رقمی کارت را بدون فاصله بفرست:")


@router.message(AddCardState.card_number)
async def add_card_number(message: Message, state: FSMContext):
    number = "".join(ch for ch in message.text if ch.isdigit())
    if len(number) != 16:
        return await message.answer("شماره کارت باید دقیقاً ۱۶ رقم باشد.")
    await state.update_data(card_number=number)
    await state.set_state(AddCardState.holder)
    await message.answer("نام صاحب کارت را بفرست:")


@router.message(AddCardState.holder)
async def add_card_holder(message: Message, state: FSMContext):
    await state.update_data(holder=message.text.strip())
    await state.set_state(AddCardState.source_id)
    await message.answer("شناسه گوشی/منبع پیامک را بفرست؛ اگر نداری علامت - را بفرست:")


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
        existing_count = len((await session.scalars(select(BankCard).where(BankCard.merchant_id == merchant.id))).all())
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
    await message.answer(f"✅ کارت ****{data['card_number'][-4:]} ثبت شد.", reply_markup=main_menu(merchant.is_admin))


@router.callback_query(F.data == "card:list")
async def list_cards(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        cards = list((await session.scalars(select(BankCard).where(BankCard.merchant_id == merchant.id))).all()) if merchant else []
    if not cards:
        text = "هنوز کارتی ثبت نشده است."
    else:
        text = "🏦 <b>کارت‌های ثبت‌شده</b>\n\n" + "\n".join(
            f"#{card.id} | {html.escape(card.bank_code)} | ****{card.card_last4} | {'فعال' if card.is_active else 'غیرفعال'}"
            for card in cards
        )
    await callback.message.edit_text(text, reply_markup=cards_menu())
    await callback.answer()


@router.callback_query(F.data == "invoice:new")
async def invoice_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ManualInvoiceState.amount)
    await callback.message.answer("مبلغ فاکتور را به تومان بفرست؛ فقط عدد:")
    await callback.answer()


@router.message(ManualInvoiceState.amount)
async def invoice_amount(message: Message, state: FSMContext):
    raw = "".join(ch for ch in message.text if ch.isdigit())
    if not raw or int(raw) < 1000:
        return await message.answer("مبلغ معتبر و حداقل ۱٬۰۰۰ تومان بفرست.")
    await state.update_data(amount_toman=int(raw))
    await state.set_state(ManualInvoiceState.description)
    await message.answer("عنوان یا توضیح فاکتور را بفرست:")


@router.message(ManualInvoiceState.description)
async def invoice_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(ManualInvoiceState.fee_mode)
    await message.answer("حالت کارمزد را بفرست: customer یا split یا merchant یا default")


@router.message(ManualInvoiceState.fee_mode)
async def invoice_fee_mode(message: Message, state: FSMContext):
    mode = message.text.strip().lower()
    if mode not in {"customer", "split", "merchant", "default"}:
        return await message.answer("یکی از این مقادیر را بفرست: customer / split / merchant / default")
    await state.update_data(fee_mode=None if mode == "default" else mode)
    await state.set_state(ManualInvoiceState.card)
    await message.answer("شناسه کارت را بفرست؛ برای انتخاب خودکار 0 را بفرست. شناسه‌ها در بخش فهرست کارت‌ها هستند.")


@router.message(ManualInvoiceState.card)
async def invoice_card(message: Message, state: FSMContext):
    raw = "".join(ch for ch in message.text if ch.isdigit())
    if not raw:
        return await message.answer("یک عدد معتبر بفرست.")
    data = await state.get_data()
    try:
        async with SessionLocal() as session:
            merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
            invoice = await create_invoice(
                session,
                merchant,
                base_amount_rial=data["amount_toman"] * 10,
                description=data["description"],
                fee_mode=data["fee_mode"],
                card_id=None if int(raw) == 0 else int(raw),
            )
            await session.commit()
        await message.answer(
            "✅ فاکتور ساخته شد\n\n"
            f"مبلغ نهایی: <b>{toman(invoice.payable_amount_rial)} تومان</b>\n"
            f"لینک پرداخت:\n{settings.base_url}/pay/{invoice.token}",
            reply_markup=main_menu(merchant.is_admin),
        )
    except Exception as exc:
        await message.answer(f"❌ ساخت فاکتور ناموفق بود:\n{html.escape(str(exc))}")
    finally:
        await state.clear()


@router.callback_query(F.data == "api")
async def api_panel(callback: CallbackQuery):
    merchant = await current_merchant(callback.from_user.id)
    prefix = merchant.api_key_prefix if merchant and merchant.api_key_prefix else "ساخته نشده"
    callback_secret = merchant.callback_secret if merchant and merchant.callback_secret else "ساخته نشده"
    await callback.message.edit_text(
        "🔑 <b>API پذیرنده</b>\n\n"
        f"پیشوند کلید فعلی: <code>{prefix}</code>\n"
        f"Secret امضای Callback: <code>{callback_secret}</code>\n\n"
        "کلید کامل API فقط هنگام ساخت نمایش داده می‌شود. برای تنظیم آدرس پیش‌فرض از دستور "
        "<code>/callback https://example.com/callback</code> استفاده کن.",
        reply_markup=api_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "api:regen")
async def api_regen(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id))
        key = await regenerate_api_key(session, merchant)
        await session.commit()
    await callback.message.answer(
        "⚠️ این کلید را همین حالا ذخیره کن؛ دوباره نمایش داده نمی‌شود:\n\n"
        f"<code>{key}</code>\n\n"
        "در درخواست API هدر X-API-Key را برابر این مقدار قرار بده."
    )
    await callback.answer("کلید جدید ساخته شد")


@router.callback_query(F.data == "sms:webhook")
async def sms_info(callback: CallbackQuery):
    async with SessionLocal() as session:
        secret = await get_setting(session, "sms_webhook_secret")
    await callback.message.answer(
        "🔗 آدرس دریافت پیامک:\n"
        f"<code>{settings.base_url}/webhooks/sms</code>\n\n"
        "هدر لازم:\n"
        f"<code>X-SMS-Secret: {secret}</code>\n\n"
        'بدنه JSON: <code>{"sender":"BANK","message":"...","device_id":"phone-1"}</code>'
    )
    await callback.answer()


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
    if value != "-" and not value.startswith(("https://", "http://")):
        return await message.answer("آدرس Callback باید با https:// یا http:// شروع شود.")
    async with SessionLocal() as session:
        merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == message.from_user.id))
        if not merchant:
            return await message.answer("ابتدا /start را بزن.")
        merchant.callback_url = None if value == "-" else value
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
