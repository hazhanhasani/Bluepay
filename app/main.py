from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.api.routes import router as api_router
from app.bot.admin import router as admin_router
from app.bot.handlers import router as bot_router
from app.core.config import settings
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models import Invoice
from app.services.invoice_service import release_invoice_reservation
from app.services.migration_service import run_runtime_migrations
from app.services.settings_service import ensure_runtime_settings
from app.services.storage_service import storage

bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.include_routers(admin_router, bot_router)


async def expiration_worker() -> None:
    while True:
        try:
            async with SessionLocal() as session:
                now = datetime.now(timezone.utc)
                invoices = list(
                    (
                        await session.scalars(
                            select(Invoice).where(Invoice.status == "pending", Invoice.expires_at < now).limit(200)
                        )
                    ).all()
                )
                for invoice in invoices:
                    await release_invoice_reservation(session, invoice, "expired")
                if invoices:
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"expiration_worker_error={type(exc).__name__}: {exc}")
        await asyncio.sleep(30)


async def telegram_worker() -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # هنگام Deploy ممکن است نسخه قبلی چند ثانیه هنوز Long Polling باشد.
            # پس از توقف نسخه قبلی، این حلقه ربات را دوباره فعال می‌کند.
            print(f"telegram_worker_error={type(exc).__name__}: {exc}")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Remote storage is best-effort and must not block Railway readiness.
    # The bounded restore runs before SQLAlchemy opens the local database.
    try:
        await asyncio.wait_for(storage.restore_if_available(), timeout=15)
    except asyncio.TimeoutError:
        storage.last_error = "TimeoutError: GitHub database restore exceeded 15 seconds"
        print(f"database_restore_error={storage.last_error}")
    except Exception as exc:
        storage.last_error = f"{type(exc).__name__}: {exc}"
        print(f"database_restore_error={storage.last_error}")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await run_runtime_migrations(engine)
    async with SessionLocal() as session:
        await ensure_runtime_settings(session)
        await session.commit()

    bot_task = asyncio.create_task(telegram_worker(), name="telegram-polling")
    expiry_task = asyncio.create_task(expiration_worker(), name="invoice-expiration")
    backup_task = asyncio.create_task(storage.retry_worker(), name="database-backup-retry")
    app.state.bot_task = bot_task
    app.state.expiry_task = expiry_task
    app.state.backup_task = backup_task
    yield

    # Make one last best-effort encrypted snapshot before shutdown.
    if storage.dirty:
        await storage.backup_now()

    for task in (bot_task, expiry_task, backup_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await bot.session.close()
    await engine.dispose()


app = FastAPI(title="Direct Payment Gateway Bot", version="0.2.8", lifespan=lifespan, docs_url="/openapi", redoc_url=None)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api_router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.port, reload=False)
