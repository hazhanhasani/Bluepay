from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx

from app.core.security import callback_signature
from app.models import Invoice, Merchant, Store
from app.version import APP_VERSION

TEST_CALLBACK_TIMEOUT_SECONDS = 10


def paid_payload(invoice: Invoice) -> dict:
    """Backward-compatible payload helper used by integrations and tests."""
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


async def _deliver_test(url: str, secret: str, payload: dict, event: str) -> tuple[bool, str]:
    """Send a one-shot connection test.

    Production paid events never pass through this helper; they are persisted in
    the durable Callback Outbox and retried by the dedicated worker.
    """
    delivery_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    headers = {
        "X-Gateway-Signature": callback_signature(payload, secret),
        "X-Gateway-Event": event,
        "X-Gateway-Delivery": delivery_id,
        "X-Gateway-Timestamp": timestamp,
        "X-Gateway-Environment": "test",
        "User-Agent": f"BluePay-Gateway/{APP_VERSION}",
    }
    try:
        async with httpx.AsyncClient(timeout=TEST_CALLBACK_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(url, json=payload, headers=headers)
        result = f"http_{response.status_code}_attempt_1"
        return 200 <= response.status_code < 300, result
    except Exception as exc:
        return False, f"{type(exc).__name__}_attempt_1"


async def send_paid_callback(invoice: Invoice) -> tuple[bool, str]:
    """Compatibility shim that ensures a durable Outbox event exists.

    Older bot/API handlers still call this after committing a payment. No
    network request is made here, so a deploy or process restart cannot lose the
    delivery. The callback worker handles the actual HTTP attempts.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from app.db.session import SessionLocal
    from app.models import CallbackEvent
    from app.services.callback_outbox_service import enqueue_live_paid_callback

    if invoice.purpose != "payment":
        return True, "internal_invoice_no_callback"
    async with SessionLocal() as session:
        existing = await session.scalar(
            select(CallbackEvent).where(CallbackEvent.event_key == f"live:invoice.paid:{invoice.id}")
        )
        if existing:
            return True, f"queued:{existing.status}"
        db_invoice = await session.scalar(
            select(Invoice).where(Invoice.id == invoice.id).options(joinedload(Invoice.store))
        )
        if not db_invoice:
            return False, "invoice_not_found"
        event = await enqueue_live_paid_callback(session, db_invoice)
        await session.commit()
        return True, "queued" if event else "callback_not_configured"


async def send_test_callback(merchant: Merchant) -> tuple[bool, str]:
    if not merchant.callback_url or not merchant.callback_secret:
        return False, "callback_not_configured"
    payload = {
        "event": "webhook.test",
        "environment": "test",
        "status": "ok",
        "merchant_id": merchant.id,
        "message": "BluePay webhook connection test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return await _deliver_test(merchant.callback_url, merchant.callback_secret, payload, "webhook.test")


async def send_store_test_callback(store: Store) -> tuple[bool, str]:
    if not store.callback_url or not store.callback_secret:
        return False, "callback_not_configured"
    payload = {
        "event": "webhook.test",
        "environment": "test",
        "status": "ok",
        "store_id": store.id,
        "store_code": store.code,
        "store_name": store.name,
        "message": "BluePay store webhook connection test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return await _deliver_test(store.callback_url, store.callback_secret, payload, "webhook.test")
