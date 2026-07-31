from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sha256_text
from app.db.session import get_session
from app.models import Merchant


async def api_merchant(
    x_api_key: str = Header(alias="X-API-Key"),
    session: AsyncSession = Depends(get_session),
) -> Merchant:
    key_hash = sha256_text(x_api_key)
    merchant = await session.scalar(
        select(Merchant).where(Merchant.api_key_hash == key_hash, Merchant.is_active.is_(True))
    )
    if not merchant:
        raise HTTPException(status_code=401, detail="API key نامعتبر است")
    return merchant
