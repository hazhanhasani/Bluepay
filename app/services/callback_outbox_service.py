from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import callback_signature
from app.models import CallbackAttempt, CallbackEvent, Invoice, SandboxInvoice
from app.version import APP_VERSION

CALLBACK_TIMEOUT_SECONDS = 10
CALLBACK_RETRY_AFTER_SECONDS = (0, 30, 300)


def live_paid_payload(invoice: Invoice) -> dict:
    return {
        "event": "invoice.paid",
        "environment": "live",
        "payment_id": invoice.token,
        "order_id": invoice.client_order_id or invoice.order_id,
        "status": invoice.status,
        "base_amount_rial": invoice.base_amount_rial,
        "fee_amount_rial": invoice.fee_amount_rial,
        "customer_fee_rial": invoice.customer_fee_rial,
        "unique_amount_rial": invoice.unique_amount_rial,
        "payable_amount_rial": invoice.payable_amount_rial,
        "reference_number": invoice.reference_number,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "store_id": invoice.store_id,
        "store_code": invoice.store.code if invoice.store else None,
        "store_name": invoice.store.name if invoice.store else None,
        "api_key_id": invoice.api_key_id,
    }


def sandbox_payload(invoice: SandboxInvoice) -> dict:
    return {
        "event": "sandbox.invoice.paid",
        "environment": "sandbox",
        "payment_id": invoice.token,
        "order_id": invoice.client_order_id,
        "status": invoice.status,
        "amount_rial": invoice.amount_rial,
        "reference_number": invoice.reference_number,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "store_id": invoice.store_id,
    }


async def enqueue_live_paid_callback(session: AsyncSession, invoice: Invoice) -> CallbackEvent | None:
    if invoice.purpose != "payment" or not invoice.callback_url or not invoice.callback_secret:
        invoice.callback_status = "not_configured" if invoice.purpose == "payment" else "skipped"
        return None
    event = CallbackEvent(
        event_key=f"live:invoice.paid:{invoice.id}",
        event_type="invoice.paid",
        environment="live",
        invoice_id=invoice.id,
        merchant_id=invoice.merchant_id,
        store_id=invoice.store_id,
        callback_url=invoice.callback_url,
        callback_secret=invoice.callback_secret,
        payload_json=json.dumps(live_paid_payload(invoice), ensure_ascii=False, sort_keys=True),
        status="pending",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=datetime.now(timezone.utc),
        delivery_id=str(uuid.uuid4()),
    )
    session.add(event)
    invoice.callback_status = "queued"
    return event


async def enqueue_sandbox_paid_callback(
    session: AsyncSession,
    invoice: SandboxInvoice,
    *,
    callback_url: str | None,
    callback_secret: str | None,
) -> CallbackEvent | None:
    if not callback_url or not callback_secret:
        invoice.callback_status = "not_configured"
        return None
    event = CallbackEvent(
        event_key=f"sandbox:invoice.paid:{invoice.id}",
        event_type="sandbox.invoice.paid",
        environment="sandbox",
        sandbox_invoice_id=invoice.id,
        merchant_id=invoice.merchant_id,
        store_id=invoice.store_id,
        callback_url=callback_url,
        callback_secret=callback_secret,
        payload_json=json.dumps(sandbox_payload(invoice), ensure_ascii=False, sort_keys=True),
        status="pending",
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=datetime.now(timezone.utc),
        delivery_id=str(uuid.uuid4()),
    )
    session.add(event)
    invoice.callback_status = "queued"
    return event


async def _claim_due_event(session: AsyncSession) -> CallbackEvent | None:
    now = datetime.now(timezone.utc)
    query = (
        select(CallbackEvent)
        .where(
            CallbackEvent.status.in_(["pending", "retry"]),
            CallbackEvent.next_attempt_at <= now,
        )
        .order_by(CallbackEvent.next_attempt_at.asc(), CallbackEvent.id.asc())
        .limit(1)
    )
    # PostgreSQL can safely skip rows claimed by another worker. SQLite ignores
    # skip_locked, so the status update below remains the final guard.
    try:
        query = query.with_for_update(skip_locked=True)
    except TypeError:
        query = query.with_for_update()
    event = await session.scalar(query)
    if not event:
        return None
    event.status = "processing"
    event.locked_at = now
    await session.flush()
    return event


async def _deliver_event(event_id: int) -> None:
    from app.db.session import SessionLocal
    async with SessionLocal() as session:
        event = await session.get(CallbackEvent, event_id)
        if not event or event.status != "processing":
            return
        payload = json.loads(event.payload_json)
        attempt_number = event.attempt_count + 1
        started = datetime.now(timezone.utc)
        started_perf = time.perf_counter()
        timestamp = started.isoformat()
        signature = callback_signature(payload, event.callback_secret)
        headers = {
            "X-Gateway-Signature": signature,
            "X-Gateway-Event": event.event_type,
            "X-Gateway-Delivery": event.delivery_id,
            "X-Gateway-Timestamp": timestamp,
            "X-Gateway-Environment": event.environment,
            "User-Agent": f"BluePay-Gateway/{APP_VERSION}",
        }
        http_status = None
        result = "unknown_error"
        preview = None
        success = False
        try:
            async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_SECONDS, follow_redirects=False) as client:
                response = await client.post(event.callback_url, json=payload, headers=headers)
            http_status = response.status_code
            preview = response.text[:1000] if response.text else None
            result = f"http_{response.status_code}"
            success = 200 <= response.status_code < 300
        except Exception as exc:
            result = type(exc).__name__
            preview = str(exc)[:1000]

        finished = datetime.now(timezone.utc)
        duration_ms = int((time.perf_counter() - started_perf) * 1000)
        session.add(
            CallbackAttempt(
                event_id=event.id,
                attempt_number=attempt_number,
                started_at=started,
                finished_at=finished,
                duration_ms=duration_ms,
                http_status=http_status,
                result=result,
                response_preview=preview,
            )
        )
        event.attempt_count = attempt_number
        event.last_result = f"{result}_attempt_{attempt_number}"
        event.locked_at = None
        if success:
            event.status = "delivered"
            event.delivered_at = finished
        elif attempt_number >= event.max_attempts:
            event.status = "failed"
        else:
            event.status = "retry"
            delay = CALLBACK_RETRY_AFTER_SECONDS[min(attempt_number, len(CALLBACK_RETRY_AFTER_SECONDS) - 1)]
            event.next_attempt_at = finished + timedelta(seconds=delay)

        if event.invoice_id:
            await session.execute(
                update(Invoice)
                .where(Invoice.id == event.invoice_id)
                .values(
                    callback_status="delivered" if success else ("failed" if event.status == "failed" else "queued"),
                    callback_last_result=event.last_result,
                    callback_attempted_at=finished,
                )
            )
        if event.sandbox_invoice_id:
            await session.execute(
                update(SandboxInvoice)
                .where(SandboxInvoice.id == event.sandbox_invoice_id)
                .values(callback_status="delivered" if success else ("failed" if event.status == "failed" else "queued"))
            )
        await session.commit()


async def process_callback_outbox_batch(batch_size: int = 30) -> int:
    from app.db.session import SessionLocal
    processed = 0
    for _ in range(batch_size):
        async with SessionLocal() as session:
            event = await _claim_due_event(session)
            if not event:
                await session.rollback()
                break
            event_id = event.id
            await session.commit()
        await _deliver_event(event_id)
        processed += 1
    return processed


async def recover_stale_callback_locks() -> int:
    from app.db.session import SessionLocal
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    async with SessionLocal() as session:
        result = await session.execute(
            update(CallbackEvent)
            .where(CallbackEvent.status == "processing", CallbackEvent.locked_at < cutoff)
            .values(status="retry", locked_at=None, next_attempt_at=datetime.now(timezone.utc))
        )
        await session.commit()
        return int(result.rowcount or 0)
