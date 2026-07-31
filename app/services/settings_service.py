from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import random_secret
from app.models import AppSetting


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


async def ensure_runtime_settings(session: AsyncSession) -> dict[str, str]:
    result = {
        "app_secret_key": await get_or_create_setting(session, "app_secret_key"),
        "encryption_key": await get_or_create_setting(session, "encryption_key"),
        "sms_webhook_secret": await get_or_create_setting(session, "sms_webhook_secret"),
    }
    await set_setting(session, "github_repository", settings.github_repository)
    await set_setting(session, "github_branch", settings.github_branch)
    await set_setting(session, "database_mode", "sqlite-encrypted-github-snapshot")
    return result
