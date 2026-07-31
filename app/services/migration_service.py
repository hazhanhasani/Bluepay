from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.storage_service import storage


async def run_runtime_migrations(engine: AsyncEngine) -> None:
    """Small zero-config migrations for SQLite installations.

    The project deliberately avoids a separate migration service so upgrades can
    still be installed from a ZIP with only BOT_TOKEN and GITHUB_TOKEN.
    """
    changed = False
    async with engine.begin() as connection:
        result = await connection.execute(text("PRAGMA table_info(merchants)"))
        merchant_columns = {str(row[1]) for row in result.fetchall()}
        if "callback_secret" in merchant_columns:
            update = await connection.execute(
                text(
                    "UPDATE merchants "
                    "SET callback_secret = lower(hex(randomblob(32))) "
                    "WHERE callback_secret IS NULL OR callback_secret = ''"
                )
            )
            changed = (update.rowcount or 0) > 0
    if changed:
        storage.mark_dirty()
