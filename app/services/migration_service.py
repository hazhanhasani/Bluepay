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

        invoice_result = await connection.execute(text("PRAGMA table_info(invoices)"))
        invoice_columns = {str(row[1]) for row in invoice_result.fetchall()}
        if "unique_amount_rial" not in invoice_columns:
            await connection.execute(
                text("ALTER TABLE invoices ADD COLUMN unique_amount_rial BIGINT NOT NULL DEFAULT 0")
            )
            changed = True
        if "purpose" not in invoice_columns:
            await connection.execute(
                text("ALTER TABLE invoices ADD COLUMN purpose VARCHAR(30) NOT NULL DEFAULT 'payment'")
            )
            changed = True
        if "wallet_target_merchant_id" not in invoice_columns:
            await connection.execute(
                text("ALTER TABLE invoices ADD COLUMN wallet_target_merchant_id BIGINT")
            )
            changed = True
        await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_invoices_purpose ON invoices (purpose)"))
        await connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_invoices_wallet_target ON invoices (wallet_target_merchant_id)")
        )

        # Protect pending invoices created by older versions as well.
        await connection.execute(
            text(
                "INSERT OR IGNORE INTO amount_reservations "
                "(card_id, invoice_token, payable_amount_rial, expires_at, created_at, updated_at) "
                "SELECT card_id, token, payable_amount_rial, expires_at, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                "FROM invoices WHERE status = 'pending'"
            )
        )
    if changed:
        storage.mark_dirty()
