from __future__ import annotations

import asyncio
import hashlib
import math
import time
from dataclasses import dataclass

from fastapi import Request


@dataclass(slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_epoch: int
    retry_after: int
    scope: str


class FixedWindowRateLimiter:
    """Small fixed-window limiter for the single Railway application process.

    The limiter deliberately stores only hashes of API keys. Counters reset on
    deployment/restart and are process-local; the documentation states this
    explicitly so operators do not mistake it for a distributed quota service.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, int], int] = {}
        self._lock = asyncio.Lock()
        self._last_cleanup_window = 0

    async def _consume(self, scope: str, identity: str, limit: int, window_seconds: int = 60) -> RateLimitDecision:
        now = time.time()
        window = int(now // window_seconds)
        reset_epoch = (window + 1) * window_seconds
        key = (scope, identity, window)
        async with self._lock:
            if window != self._last_cleanup_window:
                cutoff = window - 2
                self._counts = {k: v for k, v in self._counts.items() if k[2] >= cutoff}
                self._last_cleanup_window = window
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
        remaining = max(0, limit - count)
        retry_after = max(1, math.ceil(reset_epoch - now))
        return RateLimitDecision(
            allowed=count <= limit,
            limit=limit,
            remaining=remaining,
            reset_epoch=reset_epoch,
            retry_after=retry_after,
            scope=scope,
        )

    async def check(self, request: Request) -> tuple[RateLimitDecision | None, list[RateLimitDecision]]:
        path = request.url.path
        if not path.startswith('/api/v1'):
            return None, []

        client_ip = request.client.host if request.client else 'unknown'
        api_key = request.headers.get('x-api-key', '')
        key_identity = hashlib.sha256(api_key.encode('utf-8')).hexdigest() if api_key else ''

        decisions: list[RateLimitDecision] = []
        decisions.append(await self._consume('ip', client_ip, 300))
        if key_identity:
            decisions.append(await self._consume('api_key', key_identity, 120))
            if request.method.upper() == 'POST' and path.rstrip('/') == '/api/v1/invoices':
                decisions.append(await self._consume('invoice_create', key_identity, 30))

        blocked = next((item for item in decisions if not item.allowed), None)
        return blocked, decisions


rate_limiter = FixedWindowRateLimiter()
