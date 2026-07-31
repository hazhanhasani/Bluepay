from __future__ import annotations

import httpx
from app.core.security import callback_signature
from app.models import Invoice


async def send_paid_callback(invoice: Invoice) -> tuple[bool, str]:
    if not invoice.callback_url or not invoice.callback_secret:
        return True, "callback_not_configured"

    payload = {
        "event": "invoice.paid",
        "payment_id": invoice.token,
        "order_id": invoice.order_id,
        "status": invoice.status,
        "base_amount_rial": invoice.base_amount_rial,
        "fee_amount_rial": invoice.fee_amount_rial,
        "customer_fee_rial": invoice.customer_fee_rial,
        "payable_amount_rial": invoice.payable_amount_rial,
        "reference_number": invoice.reference_number,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
    }
    signature = callback_signature(payload, invoice.callback_secret)
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            response = await client.post(
                invoice.callback_url,
                json=payload,
                headers={"X-Gateway-Signature": signature, "User-Agent": "GatewayBot/0.1"},
            )
        return 200 <= response.status_code < 300, f"http_{response.status_code}"
    except Exception as exc:
        return False, type(exc).__name__
