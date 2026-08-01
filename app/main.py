from __future__ import annotations

import asyncio
import contextlib
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select

from app.api.errors import default_error_code, error_response
from app.api.routes import router as api_router
from app.bot.access import AccessGateMiddleware, router as access_router
from app.bot.admin import router as admin_router
from app.bot.handlers import router as bot_router
from app.core.config import settings
from app.core.rate_limit import rate_limiter
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models import Invoice
from app.services.appearance_service import load_appearance_settings
from app.services.callback_outbox_service import process_callback_outbox_batch, recover_stale_callback_locks
from app.services.idempotency_service import cleanup_expired_idempotency
from app.services.invoice_service import release_invoice_reservation
from app.services.migration_service import run_runtime_migrations
from app.services.settings_service import ensure_runtime_settings
from app.services.storage_service import storage
from app.version import APP_VERSION

bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.message.outer_middleware(AccessGateMiddleware())
dp.callback_query.outer_middleware(AccessGateMiddleware())
dp.include_routers(access_router, admin_router, bot_router)


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




async def callback_outbox_worker() -> None:
    await recover_stale_callback_locks()
    while True:
        try:
            processed = await process_callback_outbox_batch(settings.callback_worker_batch_size)
            await asyncio.sleep(0.2 if processed else settings.callback_worker_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"callback_outbox_worker_error={type(exc).__name__}: {exc}")
            await asyncio.sleep(5)


async def housekeeping_worker() -> None:
    while True:
        try:
            async with SessionLocal() as session:
                await cleanup_expired_idempotency(session)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"housekeeping_worker_error={type(exc).__name__}: {exc}")
        await asyncio.sleep(3600)


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
        await load_appearance_settings(session)
        await session.commit()

    bot_task = asyncio.create_task(telegram_worker(), name="telegram-polling")
    expiry_task = asyncio.create_task(expiration_worker(), name="invoice-expiration")
    callback_task = asyncio.create_task(callback_outbox_worker(), name="callback-outbox")
    housekeeping_task = asyncio.create_task(housekeeping_worker(), name="housekeeping")
    backup_task = asyncio.create_task(storage.retry_worker(), name="database-backup-retry")
    app.state.bot_task = bot_task
    app.state.expiry_task = expiry_task
    app.state.callback_task = callback_task
    app.state.housekeeping_task = housekeeping_task
    app.state.backup_task = backup_task
    yield

    # Make one last best-effort encrypted snapshot before shutdown.
    if storage.dirty:
        await storage.backup_now()

    for task in (bot_task, expiry_task, callback_task, housekeeping_task, backup_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await bot.session.close()
    await engine.dispose()


if settings.sentry_dsn:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment, traces_sample_rate=0.1)
    except Exception as exc:
        print(f"sentry_init_error={type(exc).__name__}: {exc}")


app = FastAPI(title="Direct Payment Gateway Bot", version=APP_VERSION, lifespan=lifespan, docs_url="/openapi", redoc_url=None)


def _is_json_contract_path(request: Request) -> bool:
    return request.url.path.startswith(("/api/", "/webhooks/"))


@app.middleware("http")
async def request_context_and_rate_limit(request: Request, call_next):
    request.state.request_id = f"req_{secrets.token_hex(12)}"
    blocked, decisions = await rate_limiter.check(request)
    if blocked:
        response = error_response(
            request,
            status_code=429,
            code="RATE_LIMITED",
            message="تعداد درخواست‌ها از سهمیه مجاز بیشتر است",
            details={
                "scope": blocked.scope,
                "limit": blocked.limit,
                "window_seconds": 60,
                "retry_after": blocked.retry_after,
            },
            headers={"Retry-After": str(blocked.retry_after)},
        )
    else:
        response = await call_next(request)

    response.headers["X-Request-ID"] = request.state.request_id
    if decisions:
        active = min(decisions, key=lambda item: (item.remaining / max(item.limit, 1), item.limit))
        response.headers["X-RateLimit-Limit"] = str(active.limit)
        response.headers["X-RateLimit-Remaining"] = str(active.remaining)
        response.headers["X-RateLimit-Reset"] = str(active.reset_epoch)
        response.headers["X-RateLimit-Scope"] = active.scope
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if not _is_json_contract_path(request):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
    message = str(exc.detail) if not isinstance(exc.detail, dict) else str(exc.detail.get("message", "درخواست نامعتبر است"))
    field = exc.detail.get("field") if isinstance(exc.detail, dict) else None
    details = exc.detail.get("details") if isinstance(exc.detail, dict) else None
    code = exc.detail.get("code") if isinstance(exc.detail, dict) else default_error_code(exc.status_code)
    return error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        field=field,
        details=details,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if not _is_json_contract_path(request):
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    errors = jsonable_encoder(exc.errors())
    first = errors[0] if errors else {}
    loc = first.get("loc") or []
    field = ".".join(str(part) for part in loc if part not in {"body", "query", "path", "header"}) or None
    code = "VALIDATION_ERROR"
    message = "پارامترهای درخواست معتبر نیستند"
    details = {"errors": errors}
    if field == "amount_toman":
        code = "INVALID_AMOUNT"
        message = "مبلغ فاکتور معتبر نیست"
        details = {"min": 1000, "max": 500000000, "errors": errors}
    elif field == "fee_mode":
        code = "INVALID_FEE_MODE"
        message = "مقدار fee_mode معتبر نیست"
        details = {"allowed": ["merchant", "customer", "split", "default"], "errors": errors}
    return error_response(
        request,
        status_code=422,
        code=code,
        message=message,
        field=field,
        details=details,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    print(f"unhandled_request_error={type(exc).__name__}: {exc}")
    if not _is_json_contract_path(request):
        return JSONResponse(status_code=500, content={"detail": "خطای داخلی سرور"})
    return error_response(
        request,
        status_code=500,
        code="INTERNAL_ERROR",
        message="خطای داخلی سرور رخ داد",
    )


app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api_router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.port, reload=False)
