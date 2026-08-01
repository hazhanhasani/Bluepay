from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import settings
from app.services.storage_service import storage


async def _sqlite_columns(connection, table: str) -> set[str]:
    result = await connection.execute(text(f"PRAGMA table_info({table})"))
    return {str(row[1]) for row in result.fetchall()}


async def _sqlite_add(connection, table: str, columns: set[str], name: str, ddl: str) -> bool:
    if name in columns:
        return False
    await connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    columns.add(name)
    return True


async def _run_sqlite_migrations(engine: AsyncEngine) -> bool:
    changed = False
    async with engine.begin() as connection:
        merchant_columns = await _sqlite_columns(connection, "merchants")
        if "callback_secret" in merchant_columns:
            update = await connection.execute(text("UPDATE merchants SET callback_secret = lower(hex(randomblob(32))) WHERE callback_secret IS NULL OR callback_secret = ''"))
            changed |= (update.rowcount or 0) > 0
        changed |= await _sqlite_add(connection, "merchants", merchant_columns, "sms_token_version", "INTEGER NOT NULL DEFAULT 1")
        changed |= await _sqlite_add(connection, "merchants", merchant_columns, "phone_number_encrypted", "TEXT")
        changed |= await _sqlite_add(connection, "merchants", merchant_columns, "phone_last4", "VARCHAR(4)")
        changed |= await _sqlite_add(connection, "merchants", merchant_columns, "phone_verified_at", "DATETIME")

        invoice_columns = await _sqlite_columns(connection, "invoices")
        invoice_additions = {
            "unique_amount_rial": "BIGINT NOT NULL DEFAULT 0",
            "purpose": "VARCHAR(30) NOT NULL DEFAULT 'payment'",
            "wallet_target_merchant_id": "BIGINT",
            "store_id": "BIGINT",
            "api_key_id": "BIGINT",
            "client_order_id": "VARCHAR(120)",
            "idempotency_key": "VARCHAR(180)",
            "environment": "VARCHAR(16) NOT NULL DEFAULT 'live'",
            "risk_score": "INTEGER NOT NULL DEFAULT 0",
            "risk_status": "VARCHAR(24) NOT NULL DEFAULT 'approved'",
            "return_url": "TEXT",
            "callback_status": "VARCHAR(24) NOT NULL DEFAULT 'not_attempted'",
            "callback_last_result": "VARCHAR(160)",
            "callback_attempted_at": "DATETIME",
        }
        for name, ddl in invoice_additions.items():
            changed |= await _sqlite_add(connection, "invoices", invoice_columns, name, ddl)
        for name, col in [
            ("ix_invoices_purpose", "purpose"),
            ("ix_invoices_wallet_target", "wallet_target_merchant_id"),
            ("ix_invoices_store_id", "store_id"),
            ("ix_invoices_api_key_id", "api_key_id"),
            ("ix_invoices_client_order_id", "client_order_id"),
            ("ix_invoices_idempotency_key", "idempotency_key"),
            ("ix_invoices_environment", "environment"),
            ("ix_invoices_risk_status", "risk_status"),
            ("ix_invoices_callback_status", "callback_status"),
        ]:
            await connection.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON invoices ({col})"))
        await connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_store_client_order ON invoices (store_id, client_order_id) WHERE store_id IS NOT NULL AND client_order_id IS NOT NULL"))
        await connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_store_idempotency ON invoices (store_id, idempotency_key) WHERE store_id IS NOT NULL AND idempotency_key IS NOT NULL"))

        store_columns = await _sqlite_columns(connection, "stores")
        changed |= await _sqlite_add(connection, "stores", store_columns, "allowed_ips", "TEXT")
        changed |= await _sqlite_add(connection, "stores", store_columns, "invoice_rate_limit_per_minute", "INTEGER")
        changed |= await _sqlite_add(connection, "stores", store_columns, "daily_amount_limit_rial", "BIGINT")

        key_columns = await _sqlite_columns(connection, "store_api_keys")
        if key_columns:
            changed |= await _sqlite_add(connection, "store_api_keys", key_columns, "is_legacy", "BOOLEAN NOT NULL DEFAULT 0")
            changed |= await _sqlite_add(connection, "store_api_keys", key_columns, "last_used_ip", "VARCHAR(64)")
            changed |= await _sqlite_add(connection, "store_api_keys", key_columns, "expires_at", "DATETIME")
        await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_store_api_keys_is_legacy ON store_api_keys (is_legacy)"))

        ledger_columns = await _sqlite_columns(connection, "wallet_ledger")
        if ledger_columns:
            for name, ddl in {
                "balance_before_rial": "BIGINT NOT NULL DEFAULT 0",
                "reserved_before_rial": "BIGINT NOT NULL DEFAULT 0",
                "reserved_after_rial": "BIGINT NOT NULL DEFAULT 0",
                "reference_type": "VARCHAR(40)",
                "reference_id": "VARCHAR(120)",
                "metadata_json": "TEXT",
                "reversed_entry_id": "BIGINT",
            }.items():
                changed |= await _sqlite_add(connection, "wallet_ledger", ledger_columns, name, ddl)
            await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_wallet_ledger_reference_type ON wallet_ledger (reference_type)"))
            await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_wallet_ledger_reference_id ON wallet_ledger (reference_id)"))
            await connection.execute(text("CREATE INDEX IF NOT EXISTS ix_wallet_ledger_reversed_entry_id ON wallet_ledger (reversed_entry_id)"))

        # Preserve old API keys and enforce one active live key per store.
        legacy_store_insert = await connection.execute(text("INSERT INTO stores (merchant_id, code, name, website_url, callback_url, callback_secret, is_active, created_at, updated_at) SELECT m.id, 'LEGACY-' || m.id, m.name || ' - فروشگاه اصلی', NULL, m.callback_url, m.callback_secret, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM merchants m WHERE m.api_key_hash IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stores s WHERE s.merchant_id = m.id)"))
        changed |= (legacy_store_insert.rowcount or 0) > 0
        legacy_key_insert = await connection.execute(text("INSERT OR IGNORE INTO store_api_keys (merchant_id, store_id, label, key_hash, key_prefix, is_active, is_legacy, last_used_at, created_at, updated_at) SELECT m.id, s.id, 'کلید قدیمی منتقل‌شده', m.api_key_hash, m.api_key_prefix, 1, 1, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM merchants m JOIN stores s ON s.merchant_id = m.id WHERE m.api_key_hash IS NOT NULL AND NOT EXISTS (SELECT 1 FROM store_api_keys k WHERE k.key_hash = m.api_key_hash)"))
        changed |= (legacy_key_insert.rowcount or 0) > 0
        duplicate_key_update = await connection.execute(text("UPDATE store_api_keys SET is_active = 0 WHERE is_active = 1 AND id NOT IN (SELECT MIN(id) FROM store_api_keys WHERE is_active = 1 GROUP BY store_id)"))
        changed |= (duplicate_key_update.rowcount or 0) > 0
        await connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_store_one_active_api_key ON store_api_keys (store_id) WHERE is_active = 1"))

        # Protect pending invoices created by older versions.
        await connection.execute(text("INSERT OR IGNORE INTO amount_reservations (card_id, invoice_token, payable_amount_rial, expires_at, created_at, updated_at) SELECT card_id, token, payable_amount_rial, expires_at, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM invoices WHERE status = 'pending'"))
    return changed


async def _run_postgres_migrations(engine: AsyncEngine) -> bool:
    statements = [
        "ALTER TABLE merchants ADD COLUMN IF NOT EXISTS sms_token_version INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE merchants ADD COLUMN IF NOT EXISTS phone_number_encrypted TEXT",
        "ALTER TABLE merchants ADD COLUMN IF NOT EXISTS phone_last4 VARCHAR(4)",
        "ALTER TABLE merchants ADD COLUMN IF NOT EXISTS phone_verified_at TIMESTAMPTZ",
        "ALTER TABLE stores ADD COLUMN IF NOT EXISTS allowed_ips TEXT",
        "ALTER TABLE stores ADD COLUMN IF NOT EXISTS invoice_rate_limit_per_minute INTEGER",
        "ALTER TABLE stores ADD COLUMN IF NOT EXISTS daily_amount_limit_rial BIGINT",
        "ALTER TABLE store_api_keys ADD COLUMN IF NOT EXISTS is_legacy BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE store_api_keys ADD COLUMN IF NOT EXISTS last_used_ip VARCHAR(64)",
        "ALTER TABLE store_api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS unique_amount_rial BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS purpose VARCHAR(30) NOT NULL DEFAULT 'payment'",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS wallet_target_merchant_id BIGINT",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS store_id BIGINT",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS api_key_id BIGINT",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS client_order_id VARCHAR(120)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(180)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS environment VARCHAR(16) NOT NULL DEFAULT 'live'",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS risk_score INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS risk_status VARCHAR(24) NOT NULL DEFAULT 'approved'",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS return_url TEXT",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS callback_status VARCHAR(24) NOT NULL DEFAULT 'not_attempted'",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS callback_last_result VARCHAR(160)",
        "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS callback_attempted_at TIMESTAMPTZ",
        "ALTER TABLE wallet_ledger ADD COLUMN IF NOT EXISTS balance_before_rial BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE wallet_ledger ADD COLUMN IF NOT EXISTS reserved_before_rial BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE wallet_ledger ADD COLUMN IF NOT EXISTS reserved_after_rial BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE wallet_ledger ADD COLUMN IF NOT EXISTS reference_type VARCHAR(40)",
        "ALTER TABLE wallet_ledger ADD COLUMN IF NOT EXISTS reference_id VARCHAR(120)",
        "ALTER TABLE wallet_ledger ADD COLUMN IF NOT EXISTS metadata_json TEXT",
        "ALTER TABLE wallet_ledger ADD COLUMN IF NOT EXISTS reversed_entry_id BIGINT",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_store_client_order ON invoices (store_id, client_order_id) WHERE store_id IS NOT NULL AND client_order_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_store_idempotency ON invoices (store_id, idempotency_key) WHERE store_id IS NOT NULL AND idempotency_key IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_store_one_active_api_key ON store_api_keys (store_id) WHERE is_active = TRUE",
    ]
    async with engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement))
    return True


async def run_runtime_migrations(engine: AsyncEngine) -> None:
    changed = await (_run_postgres_migrations(engine) if settings.is_postgres else _run_sqlite_migrations(engine))
    if changed and not settings.is_postgres:
        storage.mark_dirty()
