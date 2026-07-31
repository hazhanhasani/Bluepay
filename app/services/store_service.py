from __future__ import annotations

import secrets
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import api_key, random_secret, sha256_text
from app.models import Merchant, Store, StoreApiKey

MAX_STORES_PER_MERCHANT = 20
MAX_ACTIVE_KEYS_PER_STORE = 10
MAX_TOTAL_KEYS_PER_STORE = 30


def _store_code() -> str:
    return "ST-" + secrets.token_hex(4).upper()


async def create_store(
    session: AsyncSession,
    merchant: Merchant,
    name: str,
    website_url: str | None = None,
) -> Store:
    clean_name = " ".join((name or "").split()).strip()
    if len(clean_name) < 2:
        raise ValueError("نام فروشگاه باید حداقل ۲ نویسه باشد")
    if len(clean_name) > 120:
        raise ValueError("نام فروشگاه حداکثر ۱۲۰ نویسه است")

    count = int(await session.scalar(
        select(func.count(Store.id)).where(Store.merchant_id == merchant.id)
    ) or 0)
    if count >= MAX_STORES_PER_MERCHANT:
        raise ValueError(f"حداکثر {MAX_STORES_PER_MERCHANT} فروشگاه برای هر پذیرنده قابل ثبت است")

    for _ in range(10):
        code = _store_code()
        exists = await session.scalar(select(Store.id).where(Store.code == code))
        if not exists:
            break
    else:
        raise ValueError("ساخت شناسه فروشگاه ناموفق بود؛ دوباره تلاش کنید")

    store = Store(
        merchant_id=merchant.id,
        code=code,
        name=clean_name,
        website_url=website_url,
        callback_url=None,
        callback_secret=random_secret(32),
        is_active=True,
    )
    session.add(store)
    await session.flush()
    return store


async def issue_store_api_key(
    session: AsyncSession,
    store: Store,
    label: str | None = None,
) -> tuple[StoreApiKey, str]:
    active_count = int(await session.scalar(
        select(func.count(StoreApiKey.id)).where(
            StoreApiKey.store_id == store.id,
            StoreApiKey.is_active.is_(True),
        )
    ) or 0)
    if active_count >= MAX_ACTIVE_KEYS_PER_STORE:
        raise ValueError(f"هر فروشگاه حداکثر {MAX_ACTIVE_KEYS_PER_STORE} کلید API فعال می‌تواند داشته باشد")

    total_count = int(await session.scalar(
        select(func.count(StoreApiKey.id)).where(StoreApiKey.store_id == store.id)
    ) or 0)
    if total_count >= MAX_TOTAL_KEYS_PER_STORE:
        raise ValueError(f"هر فروشگاه حداکثر {MAX_TOTAL_KEYS_PER_STORE} کلید API ثبت‌شده می‌تواند داشته باشد")
    plain = api_key()
    key = StoreApiKey(
        merchant_id=store.merchant_id,
        store_id=store.id,
        label=(label or f"کلید API شماره {total_count + 1}")[:80],
        key_hash=sha256_text(plain),
        key_prefix=plain[:12],
        is_active=True,
        is_legacy=False,
    )
    session.add(key)
    await session.flush()
    return key, plain


async def get_owned_store(
    session: AsyncSession,
    merchant_id: int,
    store_id: int,
    *,
    with_keys: bool = False,
) -> Store | None:
    stmt = select(Store).where(Store.id == store_id, Store.merchant_id == merchant_id)
    if with_keys:
        stmt = stmt.options(selectinload(Store.api_keys))
    return await session.scalar(stmt)


async def list_merchant_stores(session: AsyncSession, merchant_id: int) -> list[Store]:
    return list((await session.scalars(
        select(Store)
        .where(Store.merchant_id == merchant_id)
        .options(selectinload(Store.api_keys))
        .order_by(Store.is_active.desc(), Store.id.asc())
    )).all())


async def set_key_active(
    session: AsyncSession,
    merchant_id: int,
    key_id: int,
    active: bool,
) -> StoreApiKey | None:
    key = await session.scalar(
        select(StoreApiKey)
        .join(Store, Store.id == StoreApiKey.store_id)
        .where(StoreApiKey.id == key_id, Store.merchant_id == merchant_id)
    )
    if not key:
        return None
    if active and not key.is_active:
        active_count = int(await session.scalar(
            select(func.count(StoreApiKey.id)).where(
                StoreApiKey.store_id == key.store_id,
                StoreApiKey.is_active.is_(True),
            )
        ) or 0)
        if active_count >= MAX_ACTIVE_KEYS_PER_STORE:
            raise ValueError(f"هر فروشگاه حداکثر {MAX_ACTIVE_KEYS_PER_STORE} کلید API فعال می‌تواند داشته باشد")
    key.is_active = active
    await session.flush()
    return key
