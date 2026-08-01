from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
)
from sqlalchemy import select

from app.bot.presentation import esc, panel, success, warning
from app.core.security import encrypt_text
from app.db.session import SessionLocal
from app.models import Merchant
from app.services.access_service import AccessDecision, RequiredChannel, clear_membership_cache, evaluate_access
from app.services.appearance_service import InlineKeyboardMarkup
from app.services.settings_service import get_setting

router = Router(name="access")


def mandatory_join_keyboard(channels: tuple[RequiredChannel, ...]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📢 عضویت در {channel.title}", url=channel.join_url)]
        for channel in channels
    ]
    rows.append([InlineKeyboardButton(text="✅ عضو شدم؛ بررسی عضویت", callback_data="access:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 ارسال شماره تلگرام من", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="برای ادامه، شماره متصل به همین حساب را ارسال کنید",
    )


def _phone_number(raw: str) -> str:
    value = (raw or "").strip()
    digits = "".join(character for character in value if character.isdigit())
    if not digits or len(digits) < 7 or len(digits) > 15:
        raise ValueError("شماره تلفن معتبر نیست")
    if value.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        return "+98" + digits[1:]
    return "+" + digits


async def show_access_prompt(event: Message | CallbackQuery, decision: AccessDecision) -> None:
    if decision.missing_channels:
        lines = [
            "برای استفاده از خدمات بلوپی، ابتدا در کانال‌های تعیین‌شده عضو شوید.",
            "پس از عضویت، دکمه «بررسی عضویت» را بزنید.",
        ]
        if decision.membership_check_failed:
            lines.extend(
                [
                    "",
                    "⚠️ بررسی یکی از کانال‌ها ممکن نشد. اگر عضو هستید، مدیر باید دسترسی ربات در کانال را بررسی کند.",
                ]
            )
        text = panel(
            "🔐",
            "تکمیل عضویت اجباری",
            lines,
            subtitle="تأیید دسترسی پیش از ورود به پنل",
            footer="عضویت باید با همین حساب تلگرام انجام شده باشد.",
        )
        markup = mandatory_join_keyboard(decision.missing_channels)
        if isinstance(event, CallbackQuery):
            try:
                await event.message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest:
                await event.message.answer(text, reply_markup=markup)
            await event.answer("عضویت شما هنوز در همه کانال‌ها تأیید نشده است.", show_alert=False)
        else:
            await event.answer(text, reply_markup=markup)
        return

    if decision.phone_required:
        text = panel(
            "📱",
            "احراز هویت شماره تلگرام",
            [
                "برای فعال‌شدن حساب، شماره‌ای را ارسال کنید که به همین حساب تلگرام متصل است.",
                "شماره فقط از طریق دکمه رسمی تلگرام پذیرفته می‌شود؛ شماره تایپ‌شده یا مخاطب دیگر معتبر نیست.",
                "اطلاعات کامل شماره به‌صورت رمزنگاری‌شده نگهداری می‌شود.",
            ],
            subtitle="تأیید مالکیت حساب کاربری",
            footer="دکمه پایین صفحه را انتخاب کنید.",
        )
        if isinstance(event, CallbackQuery):
            await event.answer()
            await event.message.answer(text, reply_markup=phone_request_keyboard())
        else:
            await event.answer(text, reply_markup=phone_request_keyboard())


async def show_home_after_access(message: Message, merchant: Merchant) -> None:
    # Imported lazily to avoid a circular import between the access gate and the
    # ordinary merchant handlers.
    from app.bot.handlers import home_view
    from app.bot.keyboards import main_menu

    text, current = await home_view(merchant.telegram_user_id)
    await message.answer(text, reply_markup=main_menu(bool(current and current.is_admin)))


class AccessGateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        user = event.from_user
        if not user:
            return await handler(event, data)

        if isinstance(event, Message):
            if event.contact is not None:
                return await handler(event, data)
            text = (event.text or "").strip().lower()
            command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
            if command == "/start":
                return await handler(event, data)
        else:
            if (event.data or "").startswith("access:"):
                return await handler(event, data)

        async with SessionLocal() as session:
            merchant = await session.scalar(select(Merchant).where(Merchant.telegram_user_id == user.id))
            if not merchant:
                if isinstance(event, CallbackQuery):
                    await event.answer("ابتدا دستور /start را ارسال کنید.", show_alert=True)
                else:
                    await event.answer("برای ایجاد حساب و ورود به پنل، دستور /start را ارسال کنید.")
                return None
            decision = await evaluate_access(session, data["bot"], merchant)

        if not decision.allowed:
            await show_access_prompt(event, decision)
            return None
        return await handler(event, data)


@router.callback_query(F.data == "access:check")
async def access_check(callback: CallbackQuery):
    async with SessionLocal() as session:
        merchant = await session.scalar(
            select(Merchant).where(Merchant.telegram_user_id == callback.from_user.id)
        )
        if not merchant:
            await callback.answer("ابتدا دستور /start را ارسال کنید.", show_alert=True)
            return
        clear_membership_cache(merchant.telegram_user_id)
        decision = await evaluate_access(
            session,
            callback.bot,
            merchant,
            force_membership_refresh=True,
        )

    if not decision.allowed:
        await show_access_prompt(callback, decision)
        return

    from app.bot.handlers import home_view
    from app.bot.keyboards import main_menu

    text, current = await home_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=main_menu(bool(current and current.is_admin)))
    await callback.answer("عضویت شما تأیید شد.")


@router.message(F.contact)
async def verify_telegram_phone(message: Message):
    async with SessionLocal() as session:
        merchant = await session.scalar(
            select(Merchant).where(Merchant.telegram_user_id == message.from_user.id)
        )
        if not merchant:
            await message.answer("ابتدا دستور /start را ارسال کنید.", reply_markup=ReplyKeyboardRemove())
            return

        # Contact messages bypass the middleware so the membership requirement is
        # checked here again before a phone number can unlock the account.
        precheck = await evaluate_access(
            session,
            message.bot,
            merchant,
            force_membership_refresh=True,
        )
        if precheck.missing_channels:
            await message.answer("ابتدا عضویت اجباری را تکمیل کنید.", reply_markup=ReplyKeyboardRemove())
            await show_access_prompt(message, precheck)
            return

        contact = message.contact
        if contact.user_id != message.from_user.id:
            await message.answer(
                warning(
                    "شماره قابل تأیید نیست",
                    "فقط شماره‌ای پذیرفته می‌شود که با دکمه «ارسال شماره تلگرام من» و از همین حساب ارسال شده باشد.",
                ),
                reply_markup=phone_request_keyboard(),
            )
            return

        try:
            normalized = _phone_number(contact.phone_number)
        except ValueError as exc:
            await message.answer(warning("شماره نامعتبر است", esc(str(exc))), reply_markup=phone_request_keyboard())
            return

        encryption_key = await get_setting(session, "encryption_key")
        if not encryption_key:
            await message.answer(
                warning("خطای تنظیمات امنیتی", "کلید رمزنگاری سامانه آماده نیست؛ دوباره تلاش کنید."),
                reply_markup=phone_request_keyboard(),
            )
            return

        merchant.phone_number_encrypted = encrypt_text(normalized, encryption_key)
        merchant.phone_last4 = normalized[-4:]
        merchant.phone_verified_at = datetime.now(timezone.utc)
        await session.commit()

    await message.answer(
        success(
            "احراز هویت با موفقیت انجام شد",
            f"شماره متصل به حساب تلگرام با پایان <code>••••{esc(normalized[-4:])}</code> تأیید شد.",
        ),
        reply_markup=ReplyKeyboardRemove(),
    )
    await show_home_after_access(message, merchant)
