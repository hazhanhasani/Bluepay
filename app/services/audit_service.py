from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def write_audit(
    session: AsyncSession,
    *,
    action: str,
    actor_type: str = "system",
    actor_id: str | int | None = None,
    merchant_id: int | None = None,
    store_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    row = AuditLog(
        actor_type=actor_type,
        actor_id=str(actor_id) if actor_id is not None else None,
        merchant_id=merchant_id,
        store_id=store_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        request_id=request_id,
        ip_address=ip_address,
        metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None,
    )
    session.add(row)
    return row
