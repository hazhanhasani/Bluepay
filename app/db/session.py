from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.core.config import settings

engine_kwargs: dict = {
    "pool_pre_ping": True,
    "echo": False,
}
if settings.is_postgres:
    engine_kwargs.update({
        "pool_size": 5,
        "max_overflow": 10,
        "pool_timeout": 30,
        "pool_recycle": 300,
    })
else:
    engine_kwargs["connect_args"] = {"timeout": 30}

engine = create_async_engine(settings.effective_database_url, **engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

_WRITE_FLAG = "_bluepay_has_database_changes"


@event.listens_for(Session, "before_flush")
def _remember_orm_changes(session: Session, _flush_context, _instances) -> None:
    """Remember ORM writes until the surrounding transaction is committed."""

    if session.new or session.dirty or session.deleted:
        session.info[_WRITE_FLAG] = True


@event.listens_for(Session, "do_orm_execute")
def _remember_bulk_changes(execute_state) -> None:
    """Track ORM UPDATE/DELETE statements that do not pass through flush."""

    if any(
        bool(getattr(execute_state, name, False))
        for name in ("is_insert", "is_update", "is_delete")
    ):
        execute_state.session.info[_WRITE_FLAG] = True


@event.listens_for(Session, "after_commit")
def _queue_sqlite_backup(session: Session) -> None:
    """Queue one encrypted snapshot after a committed root transaction.

    ``after_commit`` also fires for SAVEPOINT commits created by
    ``begin_nested()``. Clearing the write flag there could let the backup
    worker snapshot before the outer transaction commits and then miss the real
    commit completely. Keep the flag until the root transaction commits.
    """

    if session.in_nested_transaction():
        return
    changed = bool(session.info.pop(_WRITE_FLAG, False))
    if changed and not settings.is_postgres:
        from app.services.storage_service import storage

        storage.mark_dirty()


@event.listens_for(Session, "after_rollback")
def _clear_write_flag(session: Session) -> None:
    # SAVEPOINT rollback must not erase changes made earlier in the outer
    # transaction. An occasional harmless extra snapshot is safer than missing
    # a committed database change.
    if session.in_nested_transaction():
        return
    session.info.pop(_WRITE_FLAG, None)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
