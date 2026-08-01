from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sha256_text
from app.db.session import get_session
from app.models import Merchant, Store, StoreApiKey


@dataclass(slots=True)
class ApiContext:
    merchant: Merchant
    store: Store | None = None
    api_key: StoreApiKey | None = None
    legacy: bool = False


async def api_context(
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
    if row:
        key, store, merchant = row
        return ApiContext(merchant=merchant, store=store, api_key=key, legacy=bool(key.is_legacy))

    # Compatibility for installations whose migration has not yet copied the old key.
    merchant = await session.scalar(
        select(Merchant).where(Merchant.api_key_hash == key_hash, Merchant.is_active.is_(True))
    )
    if merchant:
        return ApiContext(merchant=merchant, legacy=True)
    raise HTTPException(status_code=401, detail={"code": "INVALID_API_KEY", "message": "API key نامعتبر یا غیرفعال است"})


async def api_merchant(context: ApiContext = Depends(api_context)) -> Merchant:
    return context.merchant
