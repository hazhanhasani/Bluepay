from __future__ import annotations

import argparse
import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, DateTime, func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import Base
import app.models  # noqa: F401  # register metadata


COPY_ORDER = [
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


def normalize_postgres_url(value: str) -> str:
    value = value.strip()
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        value = "postgresql+asyncpg://" + value[len("postgresql://"):]
    if not value.startswith("postgresql+asyncpg://"):
        raise ValueError("Target URL must be a PostgreSQL URL")
    return value


def source_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def convert_value(column, value: Any) -> Any:
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


def read_rows(connection: sqlite3.Connection, table_name: str, target_table) -> list[dict[str, Any]]:
    cursor = connection.execute(f'SELECT * FROM "{table_name}"')
    names = [item[0] for item in cursor.description]
    target_columns = {column.name: column for column in target_table.columns}
    rows: list[dict[str, Any]] = []
    for raw in cursor.fetchall():
        item: dict[str, Any] = {}
        for name, value in zip(names, raw):
            column = target_columns.get(name)
            if column is not None:
                item[name] = convert_value(column, value)
        # Break the two cyclic/self references while copying, then restore them.
        if table_name == "invoices" and "matched_sms_id" in item:
            item["matched_sms_id"] = None
        if table_name == "wallet_ledger" and "reversed_entry_id" in item:
            item["reversed_entry_id"] = None
        rows.append(item)
    return rows


async def ensure_empty_target(connection) -> None:
    non_empty: list[str] = []
    for table in Base.metadata.sorted_tables:
        count = int((await connection.scalar(select(func.count()).select_from(table))) or 0)
        if count:
            non_empty.append(f"{table.name}={count}")
    if non_empty:
        raise RuntimeError("Target PostgreSQL database is not empty: " + ", ".join(non_empty))


async def reset_postgres_sequences(connection) -> None:
    for table in Base.metadata.sorted_tables:
        if "id" not in table.c:
            continue
        safe_name = table.name.replace('"', '""')
        await connection.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{safe_name}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM \"{safe_name}\"), 1), "
                f"EXISTS(SELECT 1 FROM \"{safe_name}\"))"
            )
        )


async def migrate(source: Path, target_url: str) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    target_url = normalize_postgres_url(target_url)
    engine = create_async_engine(target_url, pool_pre_ping=True)
    source_db = sqlite3.connect(source)
    source_db.row_factory = sqlite3.Row
    try:
        available = source_tables(source_db)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await ensure_empty_target(connection)
            copied: dict[str, int] = {}
            for name in COPY_ORDER:
                if name not in available or name not in Base.metadata.tables:
                    continue
                table = Base.metadata.tables[name]
                rows = read_rows(source_db, name, table)
                if rows:
                    for offset in range(0, len(rows), 500):
                        await connection.execute(table.insert(), rows[offset:offset + 500])
                copied[name] = len(rows)

            # Restore references that were temporarily cleared to break cycles.
            if "invoices" in available and "sms_transactions" in available:
                source_rows = source_db.execute(
                    "SELECT id, matched_sms_id FROM invoices WHERE matched_sms_id IS NOT NULL"
                ).fetchall()
                invoice_table = Base.metadata.tables["invoices"]
                for row in source_rows:
                    await connection.execute(
                        invoice_table.update().where(invoice_table.c.id == row[0]).values(matched_sms_id=row[1])
                    )
            if "wallet_ledger" in available:
                source_rows = source_db.execute(
                    "SELECT id, reversed_entry_id FROM wallet_ledger WHERE reversed_entry_id IS NOT NULL"
                ).fetchall()
                ledger_table = Base.metadata.tables["wallet_ledger"]
                for row in source_rows:
                    await connection.execute(
                        ledger_table.update().where(ledger_table.c.id == row[0]).values(reversed_entry_id=row[1])
                    )
            await reset_postgres_sequences(connection)
        print("Migration completed successfully")
        for name, count in copied.items():
            print(f"  {name}: {count}")
    finally:
        source_db.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy an existing BluePay SQLite database to an empty PostgreSQL database")
    parser.add_argument("--source", required=True, type=Path, help="Path to gateway.db")
    parser.add_argument("--database-url", required=True, help="Empty target PostgreSQL URL")
    parser.add_argument("--confirm-empty-target", action="store_true", help="Required safety acknowledgement")
    args = parser.parse_args()
    if not args.confirm_empty_target:
        raise SystemExit("Refusing to run without --confirm-empty-target")
    asyncio.run(migrate(args.source, args.database_url))


if __name__ == "__main__":
    main()
