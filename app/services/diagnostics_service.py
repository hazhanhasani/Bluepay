from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func, select, text

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.models import CallbackEvent, Invoice, Merchant, ReconciliationCase, SmsDevice
from app.services.github_service import GitHubPublisher
from app.services.startup_service import runtime_status
from app.services.storage_service import storage


def _masked_webhook_url(url: str | None) -> str | None:
    if not url:
        return None
    marker = "/webhooks/telegram/"
    if marker in url:
        return url.split(marker, 1)[0] + marker + "[REDACTED]"
    return "[CONFIGURED]"


def _redact_sensitive(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lower = str(key).casefold()
            if any(token in lower for token in ("password", "authorization", "private_key")):
                result[key] = "[REDACTED]"
            elif lower in {"token", "secret", "api_key"} or lower.endswith(("_token", "_secret")):
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_sensitive(item)
        return result
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in (settings.bot_token, settings.github_token, settings.effective_telegram_webhook_secret):
            if secret:
                redacted = redacted.replace(str(secret), "[REDACTED]")
        return redacted
    return value


async def collect_diagnostics(*, test_github: bool = True, test_telegram=None) -> dict:
    result: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": None,
        "base_url": settings.base_url,
        "database_mode": "postgresql" if settings.is_postgres else "sqlite",
        "runtime": runtime_status.public_payload(),
        "checks": {},
    }
    try:
        from app.version import APP_VERSION
        result["version"] = APP_VERSION
    except Exception:
        result["version"] = "unknown"

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        result["checks"]["database"] = {"ok": True}
    except Exception as exc:
        result["checks"]["database"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        async with SessionLocal() as session:
            result["counts"] = {
                "merchants": int(await session.scalar(select(func.count(Merchant.id))) or 0),
                "pending_invoices": int(await session.scalar(select(func.count(Invoice.id)).where(Invoice.status == "pending")) or 0),
                "failed_callbacks": int(await session.scalar(select(func.count(CallbackEvent.id)).where(CallbackEvent.status == "failed")) or 0),
                "open_reconciliation": int(await session.scalar(select(func.count(ReconciliationCase.id)).where(ReconciliationCase.status == "open")) or 0),
                "active_sms_devices": int(await session.scalar(select(func.count(SmsDevice.id)).where(SmsDevice.is_active.is_(True))) or 0),
            }
        result["checks"]["business_queries"] = {"ok": True}
    except Exception as exc:
        result["checks"]["business_queries"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if test_telegram is not None:
        try:
            me = await test_telegram.get_me()
            webhook = await test_telegram.get_webhook_info()
            result["checks"]["telegram"] = {
                "ok": True,
                "username": me.username,
                "mode": runtime_status.telegram_mode,
                "webhook_url": _masked_webhook_url(webhook.url),
                "pending_update_count": webhook.pending_update_count,
                "last_error_message": webhook.last_error_message,
            }
        except Exception as exc:
            result["checks"]["telegram"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if test_github:
        try:
            publisher = GitHubPublisher(settings.github_token or "", settings.github_repository, settings.github_branch)
            result["checks"]["github"] = {"ok": True, **(await publisher.preflight())}
        except Exception as exc:
            result["checks"]["github"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    result["checks"]["backup"] = storage.status()
    return result


def diagnostics_bytes(payload: dict) -> bytes:
    safe_payload = _redact_sensitive(payload)
    return json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
