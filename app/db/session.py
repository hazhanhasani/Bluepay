from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine_kwargs: dict = {
    "pool_pre_ping": True,
    "echo": False,
}
if settings.is_postgres:
    # Railway PostgreSQL connections are pooled and verified before checkout.
    engine_kwargs.update({
        "pool_size": 5,
        "max_overflow": 10,
        "pool_timeout": 30,
        "pool_recycle": 300,
        "connect_args": {"server_settings": {"application_name": "bluepay"}},
    })
else:
    engine_kwargs["connect_args"] = {"timeout": 30}

engine = create_async_engine(settings.effective_database_url, **engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
