from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx


@dataclass(slots=True)
class BluePayClient:
    """Small synchronous BluePay API client.

    Keep the API key server-side. Every mutating request can carry an
    Idempotency-Key so network retries do not create duplicate operations.
    """

    api_key: str
    base_url: str
    timeout: float = 15.0

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(
                method,
                f"{self.base_url.rstrip('/')}{path}",
                headers=self._headers(idempotency_key),
                json=payload,
            )
        response.raise_for_status()
        return response.json()

    def create_invoice(
        self,
        *,
        amount_toman: int,
        order_id: str,
        description: str | None = None,
        fee_mode: str = "default",
        callback_url: str | None = None,
        ttl_minutes: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "amount_toman": amount_toman,
            "order_id": order_id,
            "description": description,
            "fee_mode": fee_mode,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if ttl_minutes is not None:
            payload["ttl_minutes"] = ttl_minutes
        return self._request(
            "POST",
            "/api/v1/invoices",
            payload=payload,
            idempotency_key=idempotency_key or f"invoice-{order_id}",
        )

    def get_invoice(self, payment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/invoices/{payment_id}")

    def get_invoice_timeline(self, payment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/invoices/{payment_id}/timeline")

    def cancel_invoice(self, payment_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/invoices/{payment_id}/cancel",
            idempotency_key=idempotency_key or f"cancel-{payment_id}",
        )

    def create_sandbox_invoice(
        self,
        *,
        amount_toman: int,
        order_id: str,
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "amount_toman": amount_toman,
            "order_id": order_id,
            "description": description,
        }
        return self._request(
            "POST",
            "/api/v1/sandbox/invoices",
            payload=payload,
            idempotency_key=idempotency_key or f"sandbox-{order_id}",
        )

    def get_sandbox_invoice(self, payment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/sandbox/invoices/{payment_id}")

    def simulate_sandbox(
        self,
        payment_id: str,
        *,
        result: Literal["paid", "failed", "expired"] = "paid",
        reference_number: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"result": result}
        if result == "paid":
            payload["reference_number"] = reference_number or f"TEST-{uuid.uuid4().hex[:12].upper()}"
        return self._request(
            "POST",
            f"/api/v1/sandbox/invoices/{payment_id}/simulate",
            payload=payload,
            idempotency_key=f"sandbox-simulate-{payment_id}-{result}",
        )

    def simulate_sandbox_paid(self, payment_id: str) -> dict[str, Any]:
        return self.simulate_sandbox(payment_id, result="paid")
