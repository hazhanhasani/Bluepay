from __future__ import annotations

import asyncio
import hashlib
import math
import time
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError


@dataclass(slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_epoch: int
    retry_after: int
    scope: str


class FixedWindowRateLimiter:
    """In-memory implementation retained for unit tests and local embedding."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, int], int] = {}
        self._lock = asyncio.Lock()

    async def _consume(self, scope: str, identity: str, limit: int, window_seconds: int = 60) -> RateLimitDecision:
        now = time.time()
        window = int(now // window_seconds)
        reset_epoch = (window + 1) * window_seconds
        key = (scope, identity, window)
        async with self._lock:
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
        return RateLimitDecision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            reset_epoch=reset_epoch,
            retry_after=max(1, math.ceil(reset_epoch - now)),
            scope=scope,
        )



class DatabaseFixedWindowRateLimiter:
    """Database-backed quota shared by all Railway replicas.

    Counters live in the primary database, so restarts and horizontal scaling do
    not reset or multiply the quota. Only hashed API keys and IP identities are
    stored. Old windows are removed opportunistically.
    """

    async def _consume(self, scope: str, identity: str, limit: int, window_seconds: int = 60) -> RateLimitDecision:
        from app.db.session import SessionLocal
        from app.models import RateLimitBucket

        now = time.time()
        window = int(now // window_seconds)
        reset_epoch = (window + 1) * window_seconds
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        count = 0
        for retry in range(2):
            async with SessionLocal() as session:
                try:
                    row = await session.scalar(
                        select(RateLimitBucket)
                        .where(
                            RateLimitBucket.scope == scope,
                            RateLimitBucket.identity_hash == identity_hash,
                            RateLimitBucket.window_start == window,
                        )
                        .with_for_update()
                    )
                    if row:
                        row.count += 1
                    else:
                        row = RateLimitBucket(scope=scope, identity_hash=identity_hash, window_start=window, count=1)
                        session.add(row)
                    count = row.count
                    if window % 10 == 0:
                        await session.execute(delete(RateLimitBucket).where(RateLimitBucket.window_start < window - 3))
                    await session.commit()
                    break
                except IntegrityError:
                    await session.rollback()
                    if retry:
                        raise
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
        if not path.startswith("/api/v1"):
            return None, []
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        client_ip = forwarded or (request.client.host if request.client else "unknown")
        api_key = request.headers.get("x-api-key", "")
        key_identity = hashlib.sha256(api_key.encode("utf-8")).hexdigest() if api_key else ""

        decisions = [await self._consume("ip", client_ip, 300)]
        if key_identity:
            decisions.append(await self._consume("api_key", key_identity, 120))
            if request.method.upper() == "POST" and path.rstrip("/") in {"/api/v1/invoices", "/api/v1/sandbox/invoices"}:
                decisions.append(await self._consume("invoice_create", key_identity, 30))
        return next((item for item in decisions if not item.allowed), None), decisions


rate_limiter = DatabaseFixedWindowRateLimiter()
