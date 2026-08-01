from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import update

from app.core.security import callback_signature
from app.models import Invoice, Merchant, Store
from app.version import APP_VERSION

CALLBACK_RETRY_DELAYS = (0, 2, 6)
CALLBACK_TIMEOUT_SECONDS = 12


def paid_payload(invoice: Invoice) -> dict:
    return {
        "event": "invoice.paid",
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


async def _record_invoice_callback(invoice_id: int, status: str, result: str | None = None) -> None:
    """Persist callback delivery state so the website and bot show the same truth."""
    # Import lazily so pure payload helpers remain testable without opening the
    # configured async SQLite driver during module import.
    from app.db.session import SessionLocal

    try:
        async with SessionLocal() as session:
            await session.execute(
                update(Invoice)
                .where(Invoice.id == invoice_id)
                .values(
                    callback_status=status,
                    callback_last_result=(result or None),
                    callback_attempted_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
    except Exception as exc:
        # Delivery must never be blocked just because the optional display state
        # could not be persisted during a temporary SQLite lock.
        print(f"callback_state_record_error={type(exc).__name__}: {exc}")


async def _deliver(url: str, secret: str, payload: dict, event: str, *, retry: bool = True) -> tuple[bool, str]:
    delivery_id = str(uuid.uuid4())
    signature = callback_signature(payload, secret)
    headers = {
        "X-Gateway-Signature": signature,
        "X-Gateway-Event": event,
        "X-Gateway-Delivery": delivery_id,
        "X-Gateway-Timestamp": datetime.now(timezone.utc).isoformat(),
        "User-Agent": f"BluePay-Gateway/{APP_VERSION}",
    }

    last_result = "not_sent"
    schedule = CALLBACK_RETRY_DELAYS if retry else (0,)
    for attempt, delay in enumerate(schedule, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_SECONDS, follow_redirects=False) as client:
                response = await client.post(url, json=payload, headers=headers)
            last_result = f"http_{response.status_code}_attempt_{attempt}"
            if 200 <= response.status_code < 300:
                return True, last_result
        except Exception as exc:
            last_result = f"{type(exc).__name__}_attempt_{attempt}"
    return False, last_result


async def send_paid_callback(invoice: Invoice) -> tuple[bool, str]:
    if invoice.purpose != "payment":
        await _record_invoice_callback(invoice.id, "skipped", "internal_invoice_no_callback")
        return True, "internal_invoice_no_callback"
    if not invoice.callback_url or not invoice.callback_secret:
        await _record_invoice_callback(invoice.id, "not_configured", "callback_not_configured")
        return True, "callback_not_configured"

    await _record_invoice_callback(invoice.id, "sending", "callback_delivery_started")
    payload = paid_payload(invoice)
    success, result = await _deliver(invoice.callback_url, invoice.callback_secret, payload, "invoice.paid")
    await _record_invoice_callback(invoice.id, "delivered" if success else "failed", result)
    return success, result


async def send_test_callback(merchant: Merchant) -> tuple[bool, str]:
    if not merchant.callback_url or not merchant.callback_secret:
        return False, "callback_not_configured"
    payload = {
        "event": "webhook.test",
        "status": "ok",
        "merchant_id": merchant.id,
        "message": "BluePay webhook connection test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return await _deliver(merchant.callback_url, merchant.callback_secret, payload, "webhook.test", retry=False)


async def send_store_test_callback(store: Store) -> tuple[bool, str]:
    if not store.callback_url or not store.callback_secret:
        return False, "callback_not_configured"
    payload = {
        "event": "webhook.test",
        "status": "ok",
        "store_id": store.id,
        "store_code": store.code,
        "store_name": store.name,
        "message": "BluePay store webhook connection test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return await _deliver(store.callback_url, store.callback_secret, payload, "webhook.test", retry=False)
