from __future__ import annotations

import secrets
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import api_key, random_secret, sha256_text
from app.models import Merchant, Store, StoreApiKey

MAX_STORES_PER_MERCHANT = 20
MAX_API_KEYS_PER_STORE = 1


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


async def get_store_api_key(session: AsyncSession, store_id: int) -> StoreApiKey | None:
    """Return the single canonical API key for a store.

    Older releases allowed multiple keys. During upgrade they are deactivated,
    but this selector still prefers the active key and then the oldest record so
    legacy databases remain manageable without deleting invoice history.
    """
    return await session.scalar(
        select(StoreApiKey)
        .where(StoreApiKey.store_id == store_id)
        .order_by(StoreApiKey.is_active.desc(), StoreApiKey.id.asc())
        .limit(1)
    )


async def issue_store_api_key(
    session: AsyncSession,
    store: Store,
    label: str | None = None,
) -> tuple[StoreApiKey, str]:
    existing = await get_store_api_key(session, store.id)
    if existing:
        raise ValueError(
            "هر فروشگاه فقط یک کلید API دارد؛ برای تعویض کلید از گزینه «بازنشانی کلید API» استفاده کنید"
        )

    plain = api_key()
    key = StoreApiKey(
        merchant_id=store.merchant_id,
        store_id=store.id,
        label=(label or "کلید اصلی")[:80],
        key_hash=sha256_text(plain),
        key_prefix=plain[:12],
        is_active=True,
        is_legacy=False,
    )
    session.add(key)
    await session.flush()
    return key, plain


async def rotate_store_api_key(
    session: AsyncSession,
    store: Store,
) -> tuple[StoreApiKey, str]:
    """Replace the store's only API key and immediately revoke all old keys."""
    key = await get_store_api_key(session, store.id)
    plain = api_key()

    if key is None:
        key = StoreApiKey(
            merchant_id=store.merchant_id,
            store_id=store.id,
            label="کلید اصلی",
            key_hash=sha256_text(plain),
            key_prefix=plain[:12],
            is_active=True,
            is_legacy=False,
        )
        session.add(key)
        await session.flush()
        return key, plain

    # Any extra records are retained only for historical invoice references, but
    # can no longer authenticate after rotation.
    await session.execute(
        update(StoreApiKey)
        .where(StoreApiKey.store_id == store.id, StoreApiKey.id != key.id)
        .values(is_active=False)
    )
    key.label = "کلید اصلی"
    key.key_hash = sha256_text(plain)
    key.key_prefix = plain[:12]
    key.is_active = True
    key.is_legacy = False
    key.last_used_at = None
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

    if active:
        # A store has one usable API. Any historical duplicate is revoked first.
        await session.execute(
            update(StoreApiKey)
            .where(StoreApiKey.store_id == key.store_id, StoreApiKey.id != key.id)
            .values(is_active=False)
        )
    key.is_active = active
    await session.flush()
    return key
