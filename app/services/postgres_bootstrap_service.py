from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, DateTime, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base
import app.models  # noqa: F401 - register SQLAlchemy metadata


CORE_COPY_ORDER = [
    "merchants",
    "stores",
    "store_api_keys",
    "bank_cards",
    "invoices",
    "amount_reservations",
    "wallet_ledger",
    "sms_transactions",
    "sms_devices",
    "merchant_team_members",
    "sandbox_invoices",
    "callback_events",
    "callback_attempts",
    "payment_events",
    "idempotency_records",
    "audit_logs",
    "risk_events",
    "reconciliation_cases",
    "rate_limit_buckets",
    "app_settings",
    "update_logs",
]


@dataclass(slots=True)
class LegacyImportResult:
    attempted: bool = False
    imported: bool = False
    source_found: bool = False
    reason: str | None = None
    rows: dict[str, int] | None = None


def _source_tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _convert_value(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, Boolean):
        return bool(value)
    if isinstance(column.type, DateTime) and not isinstance(value, datetime):
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return value


def _read_rows(connection: sqlite3.Connection, table_name: str, target_table) -> list[dict[str, Any]]:
    cursor = connection.execute(f'SELECT * FROM "{table_name}"')
    names = [item[0] for item in cursor.description]
    target_columns = {column.name: column for column in target_table.columns}
    rows: list[dict[str, Any]] = []
    for raw in cursor.fetchall():
        item: dict[str, Any] = {}
        for name, value in zip(names, raw):
            column = target_columns.get(name)
            if column is not None:
                item[name] = _convert_value(column, value)
        # Break cyclic/self references while copying and restore afterwards.
        if table_name == "invoices" and "matched_sms_id" in item:
            item["matched_sms_id"] = None
        if table_name == "wallet_ledger" and "reversed_entry_id" in item:
            item["reversed_entry_id"] = None
        if table_name == "update_logs" and "rollback_of_update_id" in item:
            item["rollback_of_update_id"] = None
        rows.append(item)
    return rows


async def ensure_database_tables(engine: AsyncEngine) -> None:
    """Create every missing table before versioned migrations run.

    ``checkfirst=True`` makes this safe for both fresh and existing databases.
    Alembic remains responsible for revision tracking and column upgrades.
    """

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("SELECT 1"))


async def _target_contains_business_data(connection) -> bool:
    merchant_table = Base.metadata.tables["merchants"]
    invoice_table = Base.metadata.tables["invoices"]
    merchants = int((await connection.scalar(select(func.count()).select_from(merchant_table))) or 0)
    invoices = int((await connection.scalar(select(func.count()).select_from(invoice_table))) or 0)
    return merchants > 0 or invoices > 0


async def _reset_sequences(connection) -> None:
    for table_name in CORE_COPY_ORDER:
        table = Base.metadata.tables.get(table_name)
        if table is None or "id" not in table.c:
            continue
        safe_name = table.name.replace('"', '""')
        await connection.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{safe_name}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM \"{safe_name}\"), 1), "
                f"EXISTS(SELECT 1 FROM \"{safe_name}\"))"
            )
        )


async def import_legacy_sqlite_if_empty(engine: AsyncEngine, source: Path | None) -> LegacyImportResult:
    result = LegacyImportResult(attempted=True, source_found=bool(source and source.is_file()))
    if not source or not source.is_file():
        result.reason = "legacy_snapshot_not_found"
        return result

    source_db = sqlite3.connect(source)
    source_db.row_factory = sqlite3.Row
    try:
        available = _source_tables(source_db)
        if "merchants" not in available:
            result.reason = "legacy_snapshot_has_no_merchants_table"
            return result

        async with engine.begin() as connection:
            if await _target_contains_business_data(connection):
                result.reason = "postgresql_already_contains_business_data"
                return result

            copied: dict[str, int] = {}
            for name in CORE_COPY_ORDER:
                if name not in available or name not in Base.metadata.tables:
                    continue
                table = Base.metadata.tables[name]
                rows = _read_rows(source_db, name, table)
                for offset in range(0, len(rows), 250):
                    batch = rows[offset:offset + 250]
                    if batch:
                        await connection.execute(pg_insert(table).values(batch).on_conflict_do_nothing())
                copied[name] = len(rows)

            # Restore references that were temporarily cleared.
            if "invoices" in available and "sms_transactions" in available:
                invoice_table = Base.metadata.tables["invoices"]
                for row in source_db.execute(
                    "SELECT id, matched_sms_id FROM invoices WHERE matched_sms_id IS NOT NULL"
                ).fetchall():
                    await connection.execute(
                        invoice_table.update().where(invoice_table.c.id == row[0]).values(matched_sms_id=row[1])
                    )
            if "wallet_ledger" in available:
                ledger_table = Base.metadata.tables["wallet_ledger"]
                for row in source_db.execute(
                    "SELECT id, reversed_entry_id FROM wallet_ledger WHERE reversed_entry_id IS NOT NULL"
                ).fetchall():
                    await connection.execute(
                        ledger_table.update().where(ledger_table.c.id == row[0]).values(reversed_entry_id=row[1])
                    )
            if "update_logs" in available:
                update_table = Base.metadata.tables["update_logs"]
                columns = {row[1] for row in source_db.execute("PRAGMA table_info(update_logs)")}
                if "rollback_of_update_id" in columns:
                    for row in source_db.execute(
                        "SELECT id, rollback_of_update_id FROM update_logs WHERE rollback_of_update_id IS NOT NULL"
                    ).fetchall():
                        await connection.execute(
                            update_table.update().where(update_table.c.id == row[0]).values(rollback_of_update_id=row[1])
                        )
            await _reset_sequences(connection)

        result.imported = True
        result.rows = copied
        result.reason = "legacy_sqlite_imported"
        return result
    finally:
        source_db.close()
