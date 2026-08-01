from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True)
class BluePayClient:
    api_key: str
    base_url: str
    timeout: float = 15.0

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def create_invoice(self, *, amount_toman: int, order_id: str, description: str | None = None, fee_mode: str = "default", idempotency_key: str | None = None) -> dict[str, Any]:
        payload = {"amount_toman": amount_toman, "order_id": order_id, "description": description, "fee_mode": fee_mode}
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url.rstrip('/')}/api/v1/invoices", headers=self._headers(idempotency_key or f"invoice-{order_id}"), json=payload)
        response.raise_for_status()
        return response.json()

    def get_invoice(self, payment_id: str) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url.rstrip('/')}/api/v1/invoices/{payment_id}", headers=self._headers())
        response.raise_for_status()
        return response.json()

    def create_sandbox_invoice(self, *, amount_toman: int, order_id: str, description: str | None = None) -> dict[str, Any]:
        payload = {"amount_toman": amount_toman, "order_id": order_id, "description": description}
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url.rstrip('/')}/api/v1/sandbox/invoices", headers=self._headers(f"sandbox-{order_id}"), json=payload)
        response.raise_for_status()
        return response.json()

    def simulate_sandbox_paid(self, payment_id: str) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url.rstrip('/')}/api/v1/sandbox/invoices/{payment_id}/simulate", headers=self._headers(), json={"result": "paid", "reference_number": f"TEST-{uuid.uuid4().hex[:12].upper()}"})
        response.raise_for_status()
        return response.json()
