from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IdempotencyRecord


def canonical_request_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        await session.delete(row)
        await session.flush()
        return None
    if row.request_hash != request_hash:
        raise ValueError("IDEMPOTENCY_KEY_REUSED")
    if row.response_json is None or row.response_status is None:
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
    session.add(row)
    await session.flush()
    return row


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
