from __future__ import annotations

import asyncio
import contextlib
import hmac
import html
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.errors import default_error_code, error_response
from app.api.routes import router as api_router
from app.bot.access import AccessGateMiddleware, router as access_router
from app.bot.admin import router as admin_router
from app.bot.handlers import router as bot_router
from app.core.config import settings
from app.core.rate_limit import rate_limiter
from app.db.session import SessionLocal, engine
from app.models import Invoice
from app.services.appearance_service import load_appearance_settings
from app.services.callback_outbox_service import process_callback_outbox_batch, recover_stale_callback_locks
from app.services.idempotency_service import cleanup_expired_idempotency
from app.services.invoice_service import release_invoice_reservation
from app.services.migration_service import run_runtime_migrations
from app.services.settings_service import ensure_runtime_settings
from app.services.startup_service import (
    lightweight_readiness_probe,
    prepare_database_with_retry,
    runtime_status,
    verify_database_and_schema,
)
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
                            select(Invoice).where(Invoice.status.in_(["pending", "partially_paid"]), Invoice.expires_at < now).limit(200)
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
    runtime_status.callback_worker_ok = True
    while True:
        try:
            processed = await process_callback_outbox_batch(settings.callback_worker_batch_size)
            runtime_status.callback_worker_ok = True
            await asyncio.sleep(0.2 if processed else settings.callback_worker_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime_status.callback_worker_ok = False
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



async def telegram_polling_worker() -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    runtime_status.telegram_mode = "polling"
    runtime_status.telegram_ok = True
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime_status.telegram_ok = False
            print(f"telegram_worker_error={type(exc).__name__}: {exc}")
            await asyncio.sleep(5)


async def notify_startup_failure(exc: BaseException) -> None:
    """Best-effort alert using only legacy-safe merchant columns."""

    admin_ids: list[int] = []
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT telegram_user_id FROM merchants WHERE is_admin = 1 LIMIT 10")
            )
            admin_ids = [int(row[0]) for row in rows.fetchall() if row[0]]
    except Exception:
        return
    error_text = html.escape(f"{type(exc).__name__}: {exc}")[:1500]
    message = (
        "🚨 <b>راه‌اندازی BluePay ناموفق بود</b>\n\n"
        f"نسخه: <code>{APP_VERSION}</code>\n"
        f"خطا: <code>{error_text}</code>\n\n"
        "نسخه در حالت تعمیر باقی مانده و مسیر /ready پاسخ 503 می‌دهد؛ "
        "Railway نباید آن را جایگزین نسخه سالم کند."
    )
    for telegram_user_id in admin_ids:
        with contextlib.suppress(Exception):
            await bot.send_message(telegram_user_id, message)


async def configure_telegram_delivery() -> asyncio.Task | None:
    me = await bot.get_me()
    if not me.id:
        raise RuntimeError("Telegram getMe returned no bot id")
    if settings.use_telegram_webhook:
        await bot.set_webhook(
            url=settings.telegram_webhook_url,
            secret_token=settings.effective_telegram_webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        runtime_status.telegram_mode = "webhook"
        runtime_status.telegram_ok = True
        return None
    return asyncio.create_task(telegram_polling_worker(), name="telegram-polling")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks: list[asyncio.Task] = []
    runtime_status.maintenance = True
    runtime_status.ready = False
    try:
        # SQLite restores remain best-effort; PostgreSQL does not use GitHub DB snapshots.
        try:
            await asyncio.wait_for(storage.restore_if_available(), timeout=15)
        except asyncio.TimeoutError:
            storage.last_error = "TimeoutError: GitHub database restore exceeded 15 seconds"
            print(f"database_restore_error={storage.last_error}")
        except Exception as exc:
            storage.last_error = f"{type(exc).__name__}: {exc}"
            print(f"database_restore_error={storage.last_error}")

        # Alembic is the primary migration mechanism. The legacy runtime
        # migrator remains as an idempotent compatibility layer for old ZIPs.
        await prepare_database_with_retry(engine)
        await run_runtime_migrations(engine)
        await verify_database_and_schema(engine)

        async with SessionLocal() as session:
            await ensure_runtime_settings(session)
            await load_appearance_settings(session)
            await session.commit()
        runtime_status.settings_ok = True

        telegram_task = await configure_telegram_delivery()
        if telegram_task:
            tasks.append(telegram_task)
        tasks.extend(
            [
                asyncio.create_task(expiration_worker(), name="invoice-expiration"),
                asyncio.create_task(callback_outbox_worker(), name="callback-outbox"),
                asyncio.create_task(housekeeping_worker(), name="housekeeping"),
                asyncio.create_task(storage.retry_worker(), name="database-backup-retry"),
            ]
        )
        runtime_status.backup_worker_ok = True
        app.state.background_tasks = tasks
        runtime_status.maintenance = False
        runtime_status.ready = True
        runtime_status.last_error = None
        yield
    except Exception as exc:
        runtime_status.mark_error(exc)
        print(f"startup_fatal_error={runtime_status.last_error}")
        await notify_startup_failure(exc)
        if settings.startup_fail_open:
            runtime_status.maintenance = False
        # Keep the HTTP process alive in maintenance mode. Railway /ready will
        # return 503, so an unhealthy release is not promoted over the old one.
        yield
    finally:
        if storage.dirty:
            with contextlib.suppress(Exception):
                await storage.backup_now()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(Exception):
            await bot.session.close()
        with contextlib.suppress(Exception):
            await engine.dispose()


if settings.sentry_dsn:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment, traces_sample_rate=0.1)
    except Exception as exc:
        print(f"sentry_init_error={type(exc).__name__}: {exc}")


app = FastAPI(title="Direct Payment Gateway Bot", version=APP_VERSION, lifespan=lifespan, docs_url="/openapi", redoc_url=None)


@app.middleware("http")
async def maintenance_gate(request: Request, call_next):
    allowed = request.url.path in {"/health", "/ready"} or request.url.path.startswith("/static/")
    if runtime_status.maintenance and not allowed:
        headers = {"Retry-After": "15", "Cache-Control": "no-store"}
        wants_json = request.url.path.startswith(("/api/", "/webhooks/")) or "application/json" in request.headers.get("accept", "")
        if wants_json:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "code": "SERVICE_MAINTENANCE",
                    "message": "سامانه در حال تکمیل مهاجرت یا بازیابی است",
                    "startup": runtime_status.public_payload(),
                },
                headers=headers,
            )
        return HTMLResponse(
            status_code=503,
            headers=headers,
            content=f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="15"><title>در حال آماده‌سازی بلوپی</title><style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#06101f;color:#eef6ff;font-family:Tahoma,Arial}}main{{width:min(520px,88%);padding:34px;border:1px solid #1d3656;border-radius:24px;background:#0b1a2f;text-align:center;box-shadow:0 30px 80px #0008}}i{{display:block;width:52px;height:52px;margin:0 auto 20px;border:5px solid #1b3e69;border-top-color:#2d83ff;border-radius:50%;animation:r 1s linear infinite}}h1{{font-size:24px}}p{{color:#90a6c1;line-height:2}}small{{color:#5e7693}}@keyframes r{{to{{transform:rotate(360deg)}}}}</style></head><body><main><i></i><h1>بلوپی در حال آماده‌سازی است</h1><p>مهاجرت دیتابیس و بررسی سلامت نسخه در حال انجام است. صفحه به‌صورت خودکار دوباره بررسی می‌شود.</p><small>نسخه {APP_VERSION}</small></main></body></html>""",
        )
    return await call_next(request)


@app.get("/health", include_in_schema=False)
async def liveness():
    return {
        "ok": True,
        "service": "gateway-bot",
        "version": APP_VERSION,
        "process": "alive",
        "ready": runtime_status.ready,
    }


@app.get("/ready", include_in_schema=False)
async def readiness():
    ok = await lightweight_readiness_probe(engine)
    status = 200 if ok and runtime_status.telegram_ok and runtime_status.callback_worker_ok else 503
    return JSONResponse(
        status_code=status,
        content={
            "ok": status == 200,
            "service": "gateway-bot",
            "version": APP_VERSION,
            "startup": runtime_status.public_payload(),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/webhooks/telegram/{path_secret}", include_in_schema=False)
async def telegram_webhook(path_secret: str, request: Request):
    expected_path = settings.effective_telegram_webhook_secret[:32]
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(path_secret, expected_path) or not hmac.compare_digest(
        header_secret, settings.effective_telegram_webhook_secret
    ):
        return JSONResponse(status_code=403, content={"ok": False})
    payload = await request.json()
    update = Update.model_validate(payload, context={"bot": bot})
    await dp.feed_update(bot, update)
    runtime_status.telegram_ok = True
    return {"ok": True}


def _is_json_contract_path(request: Request) -> bool:
    return request.url.path.startswith(("/api/", "/webhooks/"))


@app.middleware("http")
async def request_context_and_rate_limit(request: Request, call_next):
    request.state.request_id = f"req_{secrets.token_hex(12)}"
    if runtime_status.maintenance or request.url.path in {"/health", "/ready"}:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response
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
    return error_response(request, status_code=exc.status_code, code=code, message=message, field=field, details=details, headers=exc.headers)


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
    return error_response(request, status_code=422, code=code, message=message, field=field, details=details)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    print(f"unhandled_request_error={type(exc).__name__}: {exc}")
    if not _is_json_contract_path(request):
        return JSONResponse(status_code=500, content={"detail": "خطای داخلی سرور"})
    return error_response(request, status_code=500, code="INTERNAL_ERROR", message="خطای داخلی سرور رخ داد")


app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api_router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.port, reload=False)
