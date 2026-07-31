from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.services.storage_service import storage


class PersistentAsyncSession(AsyncSession):
    async def commit(self) -> None:
        has_changes = bool(self.new or self.dirty or self.deleted)
        await super().commit()
        if has_changes:
            storage.mark_dirty()


engine = create_async_engine(
    settings.normalized_database_url,
    pool_pre_ping=True,
    connect_args={"timeout": 30},
)
SessionLocal = async_sessionmaker(engine, class_=PersistentAsyncSession, expire_on_commit=False)


async def get_session():
    async with SessionLocal() as session:
        yield session
