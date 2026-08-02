from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IdempotencyRecord

PENDING_REQUEST_STALE_AFTER = timedelta(minutes=5)


def canonical_request_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def get_idempotent_response(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    request_hash: str,
) -> tuple[int, dict] | None:
    row = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if not row:
        return None

    now = datetime.now(timezone.utc)
    if _aware(row.expires_at) <= now:
        await session.delete(row)
        await session.flush()
        return None
    if row.request_hash != request_hash:
        raise ValueError("IDEMPOTENCY_KEY_REUSED")

    if row.response_json is None or row.response_status is None:
        # A process can terminate after reserving a key but before storing the
        # response. Do not block the caller for the full 24-hour TTL.
        if row.created_at and _aware(row.created_at) <= now - PENDING_REQUEST_STALE_AFTER:
            await session.delete(row)
            await session.flush()
            return None
        raise ValueError("IDEMPOTENCY_REQUEST_IN_PROGRESS")
    return row.response_status, json.loads(row.response_json)


async def reserve_idempotency(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    request_hash: str,
    ttl_hours: int = 24,
) -> IdempotencyRecord:
    row = IdempotencyRecord(
        scope=scope,
        idempotency_key=key,
        request_hash=request_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
    )
    try:
        # Keep a concurrent unique-key conflict inside a savepoint so the
        # endpoint's main transaction remains usable for a clean 409 response.
        async with session.begin_nested():
            session.add(row)
            await session.flush()
        return row
    except IntegrityError as exc:
        existing = await session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == key,
            )
        )
        if existing and existing.request_hash != request_hash:
            raise ValueError("IDEMPOTENCY_KEY_REUSED") from exc
        raise ValueError("IDEMPOTENCY_REQUEST_IN_PROGRESS") from exc


async def finalize_idempotency(
    row: IdempotencyRecord,
    *,
    status: int,
    response: dict,
    resource_type: str | None = None,
    resource_id: str | int | None = None,
) -> None:
    row.response_status = status
    row.response_json = json.dumps(response, ensure_ascii=False, sort_keys=True, default=str)
    row.resource_type = resource_type
    row.resource_id = str(resource_id) if resource_id is not None else None


async def cleanup_expired_idempotency(session: AsyncSession) -> int:
    result = await session.execute(
        delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < datetime.now(timezone.utc))
    )
    return int(result.rowcount or 0)
