from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import random_secret
from app.models import AppSetting

DEFAULT_VERIFICATION_FEE_RIAL = 20_000
DEFAULT_FEE_MODE = "merchant"
VALID_FEE_MODES = {"customer", "split", "merchant"}


async def get_setting(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = await session.get(AppSetting, key)
    return row.value if row else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row:
        row.value = value
    else:
        session.add(AppSetting(key=key, value=value))


async def get_or_create_setting(session: AsyncSession, key: str) -> str:
    value = await get_setting(session, key)
    if value:
        return value
    value = random_secret()
    await set_setting(session, key, value)
    await session.flush()
    return value


def normalize_default_fee_rial(value: str | int | None) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return DEFAULT_VERIFICATION_FEE_RIAL


def normalize_default_fee_mode(value: str | None) -> str:
    return value if value in VALID_FEE_MODES else DEFAULT_FEE_MODE


async def get_fee_defaults(session: AsyncSession) -> tuple[int, str]:
    fee_raw = await get_setting(session, "default_verification_fee_rial", str(DEFAULT_VERIFICATION_FEE_RIAL))
    mode_raw = await get_setting(session, "default_fee_mode", DEFAULT_FEE_MODE)
    return normalize_default_fee_rial(fee_raw), normalize_default_fee_mode(mode_raw)


async def set_fee_defaults(
    session: AsyncSession,
    *,
    verification_fee_rial: int | None = None,
    fee_mode: str | None = None,
) -> tuple[int, str]:
    current_fee, current_mode = await get_fee_defaults(session)
    resolved_fee = current_fee if verification_fee_rial is None else max(0, int(verification_fee_rial))
    resolved_mode = current_mode if fee_mode is None else normalize_default_fee_mode(fee_mode)
    await set_setting(session, "default_verification_fee_rial", str(resolved_fee))
    await set_setting(session, "default_fee_mode", resolved_mode)
    return resolved_fee, resolved_mode


async def ensure_runtime_settings(session: AsyncSession) -> dict[str, str]:
    result = {
        "app_secret_key": await get_or_create_setting(session, "app_secret_key"),
        "encryption_key": await get_or_create_setting(session, "encryption_key"),
        "sms_webhook_secret": await get_or_create_setting(session, "sms_webhook_secret"),
    }
    default_fee, default_mode = await get_fee_defaults(session)
    await set_setting(session, "default_verification_fee_rial", str(default_fee))
    await set_setting(session, "default_fee_mode", default_mode)
    if await get_setting(session, "access_force_join_enabled") is None:
        await set_setting(session, "access_force_join_enabled", "0")
    if await get_setting(session, "access_phone_verification_enabled") is None:
        await set_setting(session, "access_phone_verification_enabled", "0")
    if await get_setting(session, "access_required_channels") is None:
        await set_setting(session, "access_required_channels", "[]")
    await set_setting(session, "github_repository", settings.github_repository)
    await set_setting(session, "github_branch", settings.github_branch)
    await set_setting(session, "database_mode", "sqlite-encrypted-github-snapshot")
    return result
