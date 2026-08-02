from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sha256_text
from app.db.session import get_session
from app.models import Merchant, Store, StoreApiKey
from app.services.risk_service import client_ip_allowed


@dataclass(slots=True)
class ApiContext:
    merchant: Merchant
    store: Store | None = None
    api_key: StoreApiKey | None = None
    legacy: bool = False
    client_ip: str | None = None


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    return request.client.host[:64] if request.client else None


async def api_context(
    request: Request,
    x_api_key: str = Header(alias="X-API-Key"),
    session: AsyncSession = Depends(get_session),
) -> ApiContext:
    key_hash = sha256_text(x_api_key)
    row = (
        await session.execute(
            select(StoreApiKey, Store, Merchant)
            .join(Store, Store.id == StoreApiKey.store_id)
            .join(Merchant, Merchant.id == StoreApiKey.merchant_id)
            .where(
                StoreApiKey.key_hash == key_hash,
                StoreApiKey.is_active.is_(True),
                Store.is_active.is_(True),
                Store.merchant_id == Merchant.id,
                Merchant.is_active.is_(True),
            )
        )
    ).first()
    client_ip = _client_ip(request)
    if row:
        key, store, merchant = row
        now = datetime.now(timezone.utc)
        if key.expires_at:
            expires_at = key.expires_at if key.expires_at.tzinfo else key.expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                raise HTTPException(status_code=401, detail={"code": "API_KEY_EXPIRED", "message": "API key منقضی شده است"})
        if not client_ip_allowed(store, client_ip):
            raise HTTPException(status_code=403, detail={"code": "IP_NOT_ALLOWED", "message": "IP درخواست در فهرست مجاز فروشگاه نیست"})
        previous_used_at = key.last_used_at
        if previous_used_at and previous_used_at.tzinfo is None:
            previous_used_at = previous_used_at.replace(tzinfo=timezone.utc)
        should_persist_usage = (
            previous_used_at is None
            or previous_used_at <= now - timedelta(minutes=5)
            or key.last_used_ip != client_ip
        )
        if should_persist_usage:
            key.last_used_at = now
            key.last_used_ip = client_ip
            # Dependency-only updates used to be flushed and then rolled back
            # when the request-scoped session closed. Commit the throttled
            # usage metadata so the admin panel reflects real key activity.
            await session.commit()
        return ApiContext(merchant=merchant, store=store, api_key=key, legacy=bool(key.is_legacy), client_ip=client_ip)

    merchant = await session.scalar(select(Merchant).where(Merchant.api_key_hash == key_hash, Merchant.is_active.is_(True)))
    if merchant:
        return ApiContext(merchant=merchant, legacy=True, client_ip=client_ip)
    raise HTTPException(status_code=401, detail={"code": "INVALID_API_KEY", "message": "API key نامعتبر یا غیرفعال است"})


async def api_merchant(context: ApiContext = Depends(api_context)) -> Merchant:
    return context.merchant
