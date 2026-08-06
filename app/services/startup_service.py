from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class RuntimeStatus:
    started_at: str = field(default_factory=lambda: utcnow().isoformat())
    ready: bool = False
    maintenance: bool = True
    database_ok: bool = False
    migrations_ok: bool = False
    schema_ok: bool = False
    settings_ok: bool = False
    telegram_ok: bool = False
    telegram_mode: str = "starting"
    callback_worker_ok: bool = False
    backup_worker_ok: bool = False
    last_error: str | None = None
    error_trace: str | None = None
    schema_issues: list[str] = field(default_factory=list)
    migration_revision: str | None = None
    last_checked_at: str | None = None

    def mark_error(self, exc: BaseException) -> None:
        self.ready = False
        self.maintenance = True
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.error_trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:]
        self.last_checked_at = utcnow().isoformat()

    def public_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("error_trace", None)
        return payload


runtime_status = RuntimeStatus()


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return cfg


async def run_alembic_upgrade() -> str:
    """Upgrade schema to head before any business query is allowed."""

    cfg = _alembic_config()
    await asyncio.to_thread(command.upgrade, cfg, "head")
    # Alembic has already verified the revision chain. Read current revision
    # later from the database so /ready reports the exact applied revision.
    return "head"


async def _inspect_schema(engine: AsyncEngine) -> tuple[list[str], str | None]:
    async with engine.connect() as connection:
        def sync_check(sync_connection):
            inspector = inspect(sync_connection)
            existing_tables = set(inspector.get_table_names())
            issues: list[str] = []
            for table in Base.metadata.sorted_tables:
                if table.name not in existing_tables:
                    issues.append(f"missing_table:{table.name}")
                    continue
                existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name not in existing_columns:
                        issues.append(f"missing_column:{table.name}.{column.name}")
            return issues

        issues = await connection.run_sync(sync_check)
        revision = None
        try:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
        except Exception:
            revision = None
        return issues, revision


async def verify_database_and_schema(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    runtime_status.database_ok = True
    issues, revision = await _inspect_schema(engine)
    runtime_status.schema_issues = issues
    runtime_status.migration_revision = revision
    if issues:
        raise RuntimeError("Schema guard failed: " + ", ".join(issues[:30]))
    runtime_status.schema_ok = True
    runtime_status.migrations_ok = True
    runtime_status.last_checked_at = utcnow().isoformat()




async def prepare_database_with_retry(engine: AsyncEngine) -> None:
    """Run migrations and schema checks with bounded retries.

    Railway may start the application a few seconds before the Postgres service
    accepts connections. Retrying here avoids a false failed deployment while
    still keeping /ready unhealthy until the database is fully usable.
    """
    from app.core.config import settings

    attempts = max(1, int(settings.db_connect_retries) + 1)
    delay = max(0.25, float(settings.db_connect_retry_seconds))
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            await run_alembic_upgrade()
            runtime_status.migrations_ok = True
            await verify_database_and_schema(engine)
            return
        except Exception as exc:
            last_error = exc
            runtime_status.database_ok = False
            runtime_status.migrations_ok = False
            runtime_status.schema_ok = False
            runtime_status.last_error = f"{type(exc).__name__}: {exc}"
            runtime_status.last_checked_at = utcnow().isoformat()
            if attempt >= attempts:
                raise
            print(
                f"database_startup_retry attempt={attempt}/{attempts - 1} "
                f"delay_seconds={delay:g} error={type(exc).__name__}: {exc}"
            )
            await asyncio.sleep(delay)
    if last_error is not None:
        raise last_error


async def lightweight_readiness_probe(engine: AsyncEngine) -> bool:
    if not runtime_status.ready:
        return False
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        runtime_status.database_ok = True
        runtime_status.last_checked_at = utcnow().isoformat()
        return True
    except Exception as exc:
        runtime_status.database_ok = False
        runtime_status.mark_error(exc)
        return False


def diagnostics_text() -> str:
    return json.dumps(runtime_status.public_payload(), ensure_ascii=False, indent=2)
