from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Merchant
from app.services.settings_service import get_setting, set_setting

FORCE_JOIN_ENABLED_KEY = "access_force_join_enabled"
PHONE_VERIFICATION_ENABLED_KEY = "access_phone_verification_enabled"
REQUIRED_CHANNELS_KEY = "access_required_channels"


@dataclass(frozen=True, slots=True)
class RequiredChannel:
    chat_id: int
    title: str
    join_url: str
    username: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "join_url": self.join_url,
            "username": self.username,
        }


@dataclass(frozen=True, slots=True)
class AccessSettings:
    force_join_enabled: bool
    phone_verification_enabled: bool
    required_channels: tuple[RequiredChannel, ...]


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    missing_channels: tuple[RequiredChannel, ...] = ()
    phone_required: bool = False
    membership_check_failed: bool = False


_MEMBERSHIP_CACHE: dict[tuple[int, str], tuple[float, tuple[int, ...], bool]] = {}
_MEMBERSHIP_CACHE_SECONDS = 60


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _normalize_channels(raw: str | None) -> tuple[RequiredChannel, ...]:
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, list):
        return ()

    channels: list[RequiredChannel] = []
    seen: set[int] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            chat_id = int(item.get("chat_id"))
        except (TypeError, ValueError):
            continue
        title = str(item.get("title") or f"کانال {chat_id}").strip()[:120]
        join_url = str(item.get("join_url") or "").strip()
        username_raw = str(item.get("username") or "").strip().lstrip("@")
        if not join_url and username_raw:
            join_url = f"https://t.me/{username_raw}"
        if chat_id in seen or not join_url.startswith(("https://t.me/", "http://t.me/")):
            continue
        channels.append(
            RequiredChannel(
                chat_id=chat_id,
                title=title or f"کانال {chat_id}",
                join_url=join_url,
                username=username_raw or None,
            )
        )
        seen.add(chat_id)
    return tuple(channels)


async def get_access_settings(session: AsyncSession) -> AccessSettings:
    force_join = _as_bool(await get_setting(session, FORCE_JOIN_ENABLED_KEY, "0"))
    phone_verification = _as_bool(await get_setting(session, PHONE_VERIFICATION_ENABLED_KEY, "0"))
    channels = _normalize_channels(await get_setting(session, REQUIRED_CHANNELS_KEY, "[]"))
    return AccessSettings(
        force_join_enabled=force_join,
        phone_verification_enabled=phone_verification,
        required_channels=channels,
    )


async def set_force_join_enabled(session: AsyncSession, enabled: bool) -> None:
    await set_setting(session, FORCE_JOIN_ENABLED_KEY, "1" if enabled else "0")
    clear_membership_cache()


async def set_phone_verification_enabled(session: AsyncSession, enabled: bool) -> None:
    await set_setting(session, PHONE_VERIFICATION_ENABLED_KEY, "1" if enabled else "0")


async def set_required_channels(session: AsyncSession, channels: list[RequiredChannel] | tuple[RequiredChannel, ...]) -> None:
    payload = [channel.as_dict() for channel in channels]
    await set_setting(session, REQUIRED_CHANNELS_KEY, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    clear_membership_cache()


async def add_required_channel(session: AsyncSession, channel: RequiredChannel) -> tuple[RequiredChannel, ...]:
    settings = await get_access_settings(session)
    channels = [item for item in settings.required_channels if item.chat_id != channel.chat_id]
    channels.append(channel)
    await set_required_channels(session, channels)
    return tuple(channels)


async def remove_required_channel(session: AsyncSession, chat_id: int) -> tuple[RequiredChannel, ...]:
    settings = await get_access_settings(session)
    channels = [item for item in settings.required_channels if item.chat_id != int(chat_id)]
    await set_required_channels(session, channels)
    return tuple(channels)


def clear_membership_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _MEMBERSHIP_CACHE.clear()
        return
    for key in list(_MEMBERSHIP_CACHE):
        if key[0] == user_id:
            _MEMBERSHIP_CACHE.pop(key, None)


def _channel_signature(channels: tuple[RequiredChannel, ...]) -> str:
    encoded = "|".join(f"{channel.chat_id}:{channel.join_url}" for channel in channels)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _is_active_member(member: Any) -> bool:
    status_obj = getattr(member, "status", "")
    status = getattr(status_obj, "value", str(status_obj)).lower()
    if status in {"creator", "administrator", "member"}:
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def check_required_memberships(
    bot: Bot,
    user_id: int,
    channels: tuple[RequiredChannel, ...],
    *,
    force_refresh: bool = False,
) -> tuple[tuple[RequiredChannel, ...], bool]:
    if not channels:
        return (), False

    signature = _channel_signature(channels)
    cache_key = (user_id, signature)
    now = time.monotonic()
    if not force_refresh:
        cached = _MEMBERSHIP_CACHE.get(cache_key)
        if cached and cached[0] > now:
            missing_ids, failed = cached[1], cached[2]
            missing_set = set(missing_ids)
            return tuple(item for item in channels if item.chat_id in missing_set), failed

    missing: list[RequiredChannel] = []
    check_failed = False
    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel.chat_id, user_id=user_id)
            if not _is_active_member(member):
                missing.append(channel)
        except (TelegramBadRequest, TelegramForbiddenError):
            # Fail closed. The admin panel prevents adding channels where the bot
            # cannot verify members, but a later permission change must not bypass
            # mandatory membership.
            missing.append(channel)
            check_failed = True

    _MEMBERSHIP_CACHE[cache_key] = (
        now + _MEMBERSHIP_CACHE_SECONDS,
        tuple(channel.chat_id for channel in missing),
        check_failed,
    )
    return tuple(missing), check_failed


async def evaluate_access(
    session: AsyncSession,
    bot: Bot,
    merchant: Merchant,
    *,
    force_membership_refresh: bool = False,
) -> AccessDecision:
    # The primary admin must always be able to enter the panel and repair access
    # settings even if a channel is deleted or the bot loses permission.
    if merchant.is_admin:
        return AccessDecision(allowed=True)

    access = await get_access_settings(session)
    missing: tuple[RequiredChannel, ...] = ()
    check_failed = False
    if access.force_join_enabled and access.required_channels:
        missing, check_failed = await check_required_memberships(
            bot,
            merchant.telegram_user_id,
            access.required_channels,
            force_refresh=force_membership_refresh,
        )
    if missing:
        return AccessDecision(
            allowed=False,
            missing_channels=missing,
            membership_check_failed=check_failed,
        )

    phone_required = bool(access.phone_verification_enabled and not merchant.phone_verified_at)
    if phone_required:
        return AccessDecision(allowed=False, phone_required=True)
    return AccessDecision(allowed=True)
