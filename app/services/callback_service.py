from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import httpx

from app.core.security import callback_signature
from app.models import Invoice, Merchant


def paid_payload(invoice: Invoice) -> dict:
    return {
        "event": "invoice.paid",
        "payment_id": invoice.token,
        "order_id": invoice.order_id,
        "status": invoice.status,
        "base_amount_rial": invoice.base_amount_rial,
        "fee_amount_rial": invoice.fee_amount_rial,
        "customer_fee_rial": invoice.customer_fee_rial,
        "unique_amount_rial": invoice.unique_amount_rial,
        "payable_amount_rial": invoice.payable_amount_rial,
        "reference_number": invoice.reference_number,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
    }


async def _deliver(url: str, secret: str, payload: dict, event: str, *, retry: bool = True) -> tuple[bool, str]:
    delivery_id = str(uuid.uuid4())
    signature = callback_signature(payload, secret)
    headers = {
        "X-Gateway-Signature": signature,
        "X-Gateway-Event": event,
        "X-Gateway-Delivery": delivery_id,
        "X-Gateway-Timestamp": datetime.now(timezone.utc).isoformat(),
        "User-Agent": "BluePay-Gateway/0.3.3",
    }

    last_result = "not_sent"
    schedule = (0, 2, 6) if retry else (0,)
    for attempt, delay in enumerate(schedule, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
                response = await client.post(url, json=payload, headers=headers)
            last_result = f"http_{response.status_code}_attempt_{attempt}"
            if 200 <= response.status_code < 300:
                return True, last_result
        except Exception as exc:
            last_result = f"{type(exc).__name__}_attempt_{attempt}"
    return False, last_result


async def send_paid_callback(invoice: Invoice) -> tuple[bool, str]:
    if not invoice.callback_url or not invoice.callback_secret:
        return True, "callback_not_configured"
    payload = paid_payload(invoice)
    return await _deliver(invoice.callback_url, invoice.callback_secret, payload, "invoice.paid")


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
