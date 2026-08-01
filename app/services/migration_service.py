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

        if "sms_token_version" not in merchant_columns:
            await connection.execute(
                text("ALTER TABLE merchants ADD COLUMN sms_token_version INTEGER NOT NULL DEFAULT 1")
            )
            changed = True
        if "phone_number_encrypted" not in merchant_columns:
            await connection.execute(text("ALTER TABLE merchants ADD COLUMN phone_number_encrypted TEXT"))
            changed = True
        if "phone_last4" not in merchant_columns:
            await connection.execute(text("ALTER TABLE merchants ADD COLUMN phone_last4 VARCHAR(4)"))
            changed = True
        if "phone_verified_at" not in merchant_columns:
            await connection.execute(text("ALTER TABLE merchants ADD COLUMN phone_verified_at DATETIME"))
            changed = True

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
        if "store_id" not in invoice_columns:
            await connection.execute(text("ALTER TABLE invoices ADD COLUMN store_id BIGINT"))
            changed = True
        if "api_key_id" not in invoice_columns:
            await connection.execute(text("ALTER TABLE invoices ADD COLUMN api_key_id BIGINT"))
            changed = True
        if "client_order_id" not in invoice_columns:
            await connection.execute(text("ALTER TABLE invoices ADD COLUMN client_order_id VARCHAR(120)"))
            changed = True
        await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_invoices_purpose ON invoices (purpose)"))
        await connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_invoices_wallet_target ON invoices (wallet_target_merchant_id)")
        )
        await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_invoices_store_id ON invoices (store_id)"))
        await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_invoices_api_key_id ON invoices (api_key_id)"))
        await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_invoices_client_order_id ON invoices (client_order_id)"))
        await connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_store_client_order "
                "ON invoices (store_id, client_order_id) "
                "WHERE store_id IS NOT NULL AND client_order_id IS NOT NULL"
            )
        )

        key_table_result = await connection.execute(text("PRAGMA table_info(store_api_keys)"))
        key_columns = {str(row[1]) for row in key_table_result.fetchall()}
        if key_columns and "is_legacy" not in key_columns:
            await connection.execute(
                text("ALTER TABLE store_api_keys ADD COLUMN is_legacy BOOLEAN NOT NULL DEFAULT 0")
            )
            changed = True
        await connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_store_api_keys_is_legacy ON store_api_keys (is_legacy)")
        )

        # Preserve old single API keys by attaching them to a generated main store.
        # The plaintext key is not needed; the existing hash remains valid.
        legacy_store_insert = await connection.execute(
            text(
                "INSERT INTO stores "
                "(merchant_id, code, name, website_url, callback_url, callback_secret, is_active, created_at, updated_at) "
                "SELECT m.id, 'LEGACY-' || m.id, m.name || ' - فروشگاه اصلی', NULL, "
                "m.callback_url, m.callback_secret, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                "FROM merchants m "
                "WHERE m.api_key_hash IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM stores s WHERE s.merchant_id = m.id)"
            )
        )
        if (legacy_store_insert.rowcount or 0) > 0:
            changed = True

        legacy_key_insert = await connection.execute(
            text(
                "INSERT OR IGNORE INTO store_api_keys "
                "(merchant_id, store_id, label, key_hash, key_prefix, is_active, is_legacy, last_used_at, created_at, updated_at) "
                "SELECT m.id, s.id, 'کلید قدیمی منتقل‌شده', m.api_key_hash, m.api_key_prefix, 1, 1, NULL, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                "FROM merchants m JOIN stores s ON s.merchant_id = m.id "
                "WHERE m.api_key_hash IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM store_api_keys k WHERE k.key_hash = m.api_key_hash)"
            )
        )
        if (legacy_key_insert.rowcount or 0) > 0:
            changed = True

        # Releases before 0.5.6 allowed multiple active API keys per store.
        # Keep one active key for compatibility and revoke the rest without
        # deleting rows that may still be referenced by historical invoices.
        duplicate_key_update = await connection.execute(
            text(
                "UPDATE store_api_keys SET is_active = 0 "
                "WHERE is_active = 1 AND id NOT IN ("
                "SELECT MIN(id) FROM store_api_keys WHERE is_active = 1 GROUP BY store_id"
                ")"
            )
        )
        if (duplicate_key_update.rowcount or 0) > 0:
            changed = True
        await connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_store_one_active_api_key "
                "ON store_api_keys (store_id) WHERE is_active = 1"
            )
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
