from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice, PaymentEvent


async def record_payment_event(
    session: AsyncSession,
    invoice: Invoice,
    event_type: str,
    *,
    status: str = "recorded",
    actor_type: str = "system",
    actor_id: str | int | None = None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> PaymentEvent:
    row = PaymentEvent(
        invoice_id=invoice.id,
        merchant_id=invoice.merchant_id,
        store_id=invoice.store_id,
        event_type=event_type[:80],
        status=status[:32],
        actor_type=actor_type[:32],
        actor_id=str(actor_id)[:120] if actor_id is not None else None,
        request_id=request_id[:80] if request_id else None,
        detail_json=json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
    )
    session.add(row)
    await session.flush()
    return row


async def invoice_timeline(session: AsyncSession, invoice_id: int, *, limit: int = 100) -> list[PaymentEvent]:
    return list(
        (
            await session.scalars(
                select(PaymentEvent)
                .where(PaymentEvent.invoice_id == invoice_id)
                .order_by(PaymentEvent.id.asc())
                .limit(max(1, min(limit, 500)))
            )
        ).all()
    )
