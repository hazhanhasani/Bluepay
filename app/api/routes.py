from __future__ import annotations

import hmac
import ipaddress
import httpx
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiContext, api_context
from app.api.errors import error_response
from app.api.schemas import (CreateInvoiceRequest, SandboxCreateInvoiceRequest, SandboxSimulationRequest, SmsDevicePolicyRequest, StoreSecurityRequest)
from app.core.config import settings
from app.core.security import decrypt_text
from app.core.urls import validate_public_https_url
from app.db.session import get_session
from app.models import Invoice, Merchant, SandboxInvoice, SmsTransaction, Store
from app.parsers import BANK_PROFILES, bank_label
from app.services.callback_service import send_paid_callback
from app.services.audit_service import write_audit
from app.services.callback_outbox_service import enqueue_sandbox_paid_callback
from app.services.idempotency_service import (
    canonical_request_hash, finalize_idempotency, get_idempotent_response, reserve_idempotency,
)
from app.services.metrics_service import prometheus_metrics
from app.services.portal_service import build_portal_dashboard, verify_portal_token
from app.services.reconciliation_service import open_reconciliation_case
from app.services.risk_service import evaluate_invoice_creation
from app.services.integration_service import (
    merchant_docs_url,
    merchant_sms_token,
    merchant_sms_webhook_url,
)
from app.services.invoice_service import create_invoice, get_invoice_by_token, release_invoice_reservation
from app.services.public_dashboard_service import build_public_dashboard
from app.services.settings_service import get_setting, set_setting
from app.services.sms_service import ingest_sms
from app.services.sms_notification_service import send_invalid_sms_payload_notice, send_sms_processing_notice
from app.services.sms_payload_service import SmsPayloadError, parse_sms_payload
from app.services.storage_service import storage
from app.version import APP_VERSION

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
_BOT_USERNAME_CACHE: str | None = None


def rial_to_toman(value: int) -> int:
    return value // 10


async def _read_sms_webhook_payload(request: Request) -> tuple[str, str, str | None, str | None]:
    raw_body = await request.body()
    raw_text = raw_body.decode("utf-8", errors="replace")
    return parse_sms_payload(
        raw_text,
        request.headers.get("content-type") or "",
        dict(request.query_params),
    )


def invoice_payload(invoice: Invoice) -> dict:
    return {
        "payment_id": invoice.token,
        "order_id": invoice.client_order_id or invoice.order_id,
        "status": invoice.status,
        "purpose": invoice.purpose,
        "fee_mode": invoice.fee_mode,
        "base_amount_rial": invoice.base_amount_rial,
        "fee_amount_rial": invoice.fee_amount_rial,
        "customer_fee_rial": invoice.customer_fee_rial,
        "unique_amount_rial": invoice.unique_amount_rial,
        "payable_amount_rial": invoice.payable_amount_rial,
        "payment_url": f"{settings.base_url}/pay/{invoice.token}",
        "return_url": invoice.return_url,
        "expires_at": invoice.expires_at.isoformat(),
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "reference_number": invoice.reference_number,
        "store_id": invoice.store_id,
        "api_key_id": invoice.api_key_id,
    }


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)):
    from app.models import CallbackEvent
    backup = storage.status()
    pending_callbacks = int(await session.scalar(select(func.count(CallbackEvent.id)).where(CallbackEvent.status.in_(["pending", "retry", "processing"]))) or 0)
    failed_callbacks = int(await session.scalar(select(func.count(CallbackEvent.id)).where(CallbackEvent.status == "failed")) or 0)
    return {
        "ok": True,
        "storage_ok": True if settings.is_postgres else backup["last_error"] is None,
        "service": "gateway-bot",
        "version": APP_VERSION,
        "environment": settings.environment,
        "database": "postgresql" if settings.is_postgres else "sqlite-encrypted-github",
        "callback_outbox": {"pending": pending_callbacks, "failed": failed_callbacks},
        "backup": None if settings.is_postgres else backup,
    }


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, session: AsyncSession = Depends(get_session)):
    live_data = await build_public_dashboard(session)
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "base_url": settings.base_url,
            "docs_url": f"{settings.base_url}/developers",
            "telegram_url": f"{settings.base_url}/telegram",
            "health_url": f"{settings.base_url}/health",
            "live_url": f"{settings.base_url}/public/live",
            "live_data": live_data,
            "app_version": APP_VERSION,
        },
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/public/live", include_in_schema=False)
async def public_live_dashboard(session: AsyncSession = Depends(get_session)):
    payload = await build_public_dashboard(session)
    return JSONResponse(
        content=payload,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/status", response_class=HTMLResponse, include_in_schema=False)
async def public_status(request: Request, session: AsyncSession = Depends(get_session)):
    from app.models import CallbackEvent
    pending = int(await session.scalar(select(func.count(Invoice.id)).where(Invoice.status == "pending")) or 0)
    failed_callbacks = int(await session.scalar(select(func.count(CallbackEvent.id)).where(CallbackEvent.status == "failed")) or 0)
    review_sms = int(await session.scalar(select(func.count(SmsTransaction.id)).where(SmsTransaction.status.in_(["review", "unmatched"]))) or 0)
    status = "operational" if failed_callbacks == 0 else "degraded"
    return templates.TemplateResponse(
        "status.html",
        {"request": request, "status": status, "pending": pending, "failed_callbacks": failed_callbacks, "review_sms": review_sms, "app_version": APP_VERSION, "database": "PostgreSQL" if settings.is_postgres else "SQLite"},
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/telegram", include_in_schema=False)
async def open_telegram_bot():
    """Resolve the bot username without requiring a third installation variable."""
    global _BOT_USERNAME_CACHE
    if not _BOT_USERNAME_CACHE:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"https://api.telegram.org/bot{settings.bot_token}/getMe")
                response.raise_for_status()
                payload = response.json()
            username = str((payload.get("result") or {}).get("username") or "").strip().lstrip("@")
            if username:
                _BOT_USERNAME_CACHE = username
        except Exception as exc:
            print(f"telegram_link_resolve_error={type(exc).__name__}: {exc}")
    if _BOT_USERNAME_CACHE:
        return RedirectResponse(
            url=f"https://t.me/{_BOT_USERNAME_CACHE}",
            status_code=302,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    return RedirectResponse(url=f"{settings.base_url}/developers", status_code=302)


@router.get("/downloads/sms-forwarder", include_in_schema=False)
async def download_sms_forwarder():
    """Stable BluePay link to the official SMS Forwarder listing.

    The project intentionally does not redistribute modified or third-party APK
    binaries. Keeping this as a redirect lets the bot and documentation use a
    stable BluePay URL while installation stays on the publisher's official
    channel.
    """
    return RedirectResponse(
        url="https://play.google.com/store/apps/details?id=com.frzinapps.smsforward",
        status_code=302,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/developers", response_class=HTMLResponse)
async def developer_docs(request: Request):
    return templates.TemplateResponse(
        "developers.html",
        {
            "request": request,
            "base_url": settings.base_url,
            "sms_webhook_url": f"{settings.base_url}/webhooks/sms/MERCHANT_ID/••••••••••••",
            "banks": BANK_PROFILES,
            "app_version": APP_VERSION,
            "docs_contract_version": "2026-08-01.4",
            "sms_forwarder_download_url": f"{settings.base_url}/downloads/sms-forwarder",
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
    )


@router.get("/developers/{merchant_id}/{token}", include_in_schema=False)
async def legacy_personalized_developer_docs(merchant_id: int, token: str):
    """Retire old personalized documentation links without exposing account data."""
    return RedirectResponse(
        url=f"{settings.base_url}/developers",
        status_code=307,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/api/v1/banks")
async def api_banks():
    return {
        "success": True,
        "count": len(BANK_PROFILES),
        "banks": [
            {"code": profile.code, "name": profile.label, "legacy": profile.legacy}
            for profile in BANK_PROFILES
        ],
    }


@router.get("/api/v1/account")
async def api_account(context: ApiContext = Depends(api_context)):
    merchant = context.merchant
    store = context.store
    callback_url = (store.callback_url or merchant.callback_url) if store else merchant.callback_url
    return {
        "success": True,
        "merchant_id": merchant.id,
        "name": merchant.name,
        "merchant_name": merchant.name,
        "store": ({
            "id": store.id,
            "code": store.code,
            "name": store.name,
            "website_url": store.website_url,
            "active": store.is_active,
        } if store else None),
        "api_key": ({
            "id": context.api_key.id,
            "label": context.api_key.label,
            "prefix": context.api_key.key_prefix,
            "legacy": context.legacy,
        } if context.api_key else {"legacy": True}),
        "fee_mode": merchant.fee_mode,
        "verification_fee_rial": merchant.verification_fee_rial,
        "fee_enabled": merchant.verification_fee_rial > 0,
        "available_balance_rial": merchant.available_balance_rial,
        "callback_configured": bool(callback_url),
        "callback_url": callback_url,
        "sms_webhook_url": merchant_sms_webhook_url(merchant),
        "developer_docs_url": merchant_docs_url(merchant),
    }


@router.get("/api/v1/store/security")
async def api_store_security(context: ApiContext = Depends(api_context)):
    if not context.store:
        raise HTTPException(status_code=400, detail={"code": "STORE_REQUIRED", "message": "کلید فروشگاهی لازم است"})
    return {
        "success": True,
        "store_id": context.store.id,
        "allowed_ips": [item.strip() for item in (context.store.allowed_ips or "").split(",") if item.strip()],
        "invoice_rate_limit_per_minute": context.store.invoice_rate_limit_per_minute or 30,
        "daily_amount_limit_toman": (context.store.daily_amount_limit_rial // 10) if context.store.daily_amount_limit_rial else 500_000_000,
    }


@router.put("/api/v1/store/security")
async def api_update_store_security(
    request: Request,
    body: StoreSecurityRequest,
    context: ApiContext = Depends(api_context),
    session: AsyncSession = Depends(get_session),
):
    if not context.store:
        raise HTTPException(status_code=400, detail={"code": "STORE_REQUIRED", "message": "کلید فروشگاهی لازم است"})
    normalized: list[str] = []
    for item in body.allowed_ips:
        try:
            normalized.append(str(ipaddress.ip_network(item.strip(), strict=False)))
        except ValueError:
            raise HTTPException(status_code=422, detail={"code": "INVALID_IP_NETWORK", "message": f"IP یا شبکه نامعتبر است: {item}", "field": "allowed_ips"})
    if normalized and context.client_ip:
        current = ipaddress.ip_address(context.client_ip)
        if not any(current in ipaddress.ip_network(item, strict=False) for item in normalized):
            raise HTTPException(status_code=400, detail={"code": "CURRENT_IP_WOULD_BE_BLOCKED", "message": "IP فعلی باید در فهرست مجاز باقی بماند تا دسترسی قطع نشود", "details": {"current_ip": context.client_ip}})
    context.store.allowed_ips = ",".join(normalized) or None
    context.store.invoice_rate_limit_per_minute = body.invoice_rate_limit_per_minute
    context.store.daily_amount_limit_rial = body.daily_amount_limit_toman * 10 if body.daily_amount_limit_toman else None
    await write_audit(
        session,
        action="store.security.updated",
        actor_type="api_key",
        actor_id=context.api_key.id if context.api_key else "legacy",
        merchant_id=context.merchant.id,
        store_id=context.store.id,
        entity_type="store",
        entity_id=context.store.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=context.client_ip,
        metadata={"allowed_ips": normalized, "rate_limit": body.invoice_rate_limit_per_minute, "daily_amount_limit_toman": body.daily_amount_limit_toman},
    )
    await session.commit()
    return {"success": True, "message": "سیاست امنیتی فروشگاه ذخیره شد"}


@router.get("/api/v1/account/sms-devices")
async def api_sms_devices(context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    raw = await get_setting(session, f"sms_trusted_devices:{context.merchant.id}") or ""
    return {"success": True, "devices": [item.strip() for item in raw.split(",") if item.strip()]}


@router.put("/api/v1/account/sms-devices")
async def api_update_sms_devices(
    request: Request,
    body: SmsDevicePolicyRequest,
    context: ApiContext = Depends(api_context),
    session: AsyncSession = Depends(get_session),
):
    devices = []
    for item in body.devices:
        value = " ".join(item.strip().split())[:120]
        if value and value.casefold() not in {x.casefold() for x in devices}:
            devices.append(value)
    await set_setting(session, f"sms_trusted_devices:{context.merchant.id}", ",".join(devices))
    await write_audit(
        session,
        action="sms.devices.updated",
        actor_type="api_key",
        actor_id=context.api_key.id if context.api_key else "legacy",
        merchant_id=context.merchant.id,
        store_id=context.store.id if context.store else None,
        entity_type="merchant",
        entity_id=context.merchant.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=context.client_ip,
        metadata={"device_count": len(devices)},
    )
    await session.commit()
    return {"success": True, "devices": devices}


@router.post("/api/v1/invoices")
async def api_create_invoice(
    request: Request,
    body: CreateInvoiceRequest,
    context: ApiContext = Depends(api_context),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
):
    merchant = context.merchant
    store = context.store
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not 8 <= len(idempotency_key) <= 180:
            raise HTTPException(status_code=400, detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "Idempotency-Key باید بین ۸ تا ۱۸۰ نویسه باشد", "field": "Idempotency-Key"})

    request_payload = body.model_dump(mode="json")
    request_hash = canonical_request_hash(request_payload)
    scope = f"live:create_invoice:{store.id if store else merchant.id}"
    idem_row = None
    if idempotency_key:
        try:
            existing = await get_idempotent_response(session, scope=scope, key=idempotency_key, request_hash=request_hash)
        except ValueError as exc:
            code = str(exc)
            if code == "IDEMPOTENCY_KEY_REUSED":
                raise HTTPException(status_code=409, detail={"code": code, "message": "این Idempotency-Key قبلاً با بدنه دیگری استفاده شده است"})
            raise HTTPException(status_code=409, detail={"code": code, "message": "درخواست هم‌زمان با همین Idempotency-Key در حال پردازش است"})
        if existing:
            status, response_payload = existing
            return JSONResponse(status_code=status, content=response_payload, headers={"Idempotency-Replayed": "true"})
        idem_row = await reserve_idempotency(session, scope=scope, key=idempotency_key, request_hash=request_hash)

    risk = await evaluate_invoice_creation(
        session,
        merchant_id=merchant.id,
        store=store,
        amount_rial=body.amount_toman * 10,
        client_ip=context.client_ip,
    )
    if not risk.allowed:
        await session.commit()
        raise HTTPException(status_code=403, detail={"code": risk.rule_code, "message": risk.detail, "details": {"risk_score": risk.score}})

    try:
        callback_url = str(body.callback_url) if body.callback_url else None
        if callback_url:
            valid, normalized = validate_public_https_url(callback_url)
            if not valid:
                raise ValueError(normalized)
            callback_url = normalized
        return_url = str(body.return_url) if body.return_url else None
        if return_url:
            valid, normalized = validate_public_https_url(return_url)
            if not valid:
                raise ValueError(normalized)
            return_url = normalized
        invoice = await create_invoice(
            session,
            merchant=merchant,
            base_amount_rial=body.amount_toman * 10,
            description=body.description,
            order_id=(f"API-{store.id}-{secrets.token_hex(8).upper()}" if store and body.order_id else body.order_id),
            client_order_id=body.order_id if store else None,
            fee_mode=body.fee_mode,
            card_id=body.card_id,
            callback_url=callback_url or (store.callback_url if store else merchant.callback_url),
            return_url=return_url,
            callback_secret=(store.callback_secret if store and (callback_url or store.callback_url) else (merchant.callback_secret if not store else None)),
            ttl_minutes=body.ttl_minutes,
            store_id=store.id if store else None,
            api_key_id=context.api_key.id if context.api_key else None,
            idempotency_key=idempotency_key,
            risk_score=risk.score,
            risk_status=risk.status,
        )
        payload = invoice_payload(invoice)
        payload["risk"] = {"score": risk.score, "status": risk.status}
        if store:
            payload["store_code"] = store.code
            payload["store_name"] = store.name
        response_payload = {"success": True, **payload}
        await write_audit(
            session,
            action="invoice.created",
            actor_type="api_key",
            actor_id=context.api_key.id if context.api_key else "legacy",
            merchant_id=merchant.id,
            store_id=store.id if store else None,
            entity_type="invoice",
            entity_id=invoice.id,
            request_id=getattr(request.state, "request_id", None),
            ip_address=context.client_ip,
            metadata={"amount_rial": invoice.base_amount_rial, "risk_score": risk.score},
        )
        if idem_row:
            await finalize_idempotency(idem_row, status=200, response=response_payload, resource_type="invoice", resource_id=invoice.id)
        await session.commit()
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return JSONResponse(status_code=200, content=response_payload, headers=headers)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail={"code": "ORDER_OR_IDEMPOTENCY_CONFLICT", "message": "order_id یا Idempotency-Key قبلاً در این فروشگاه استفاده شده است"})
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail={"code": "INVOICE_CREATE_FAILED", "message": str(exc)})


@router.get("/api/v1/invoices/{token}")
async def api_invoice_status(
    token: str,
    context: ApiContext = Depends(api_context),
    session: AsyncSession = Depends(get_session),
):
    conditions = [Invoice.token == token, Invoice.merchant_id == context.merchant.id]
    if context.store and not context.legacy:
        conditions.append(Invoice.store_id == context.store.id)
    invoice = await session.scalar(select(Invoice).where(*conditions))
    if not invoice:
        raise HTTPException(status_code=404, detail={"code": "INVOICE_NOT_FOUND", "message": "فاکتور یافت نشد"})
    payload = invoice_payload(invoice)
    if context.store:
        payload.update({"store_code": context.store.code, "store_name": context.store.name})
    return {"success": True, **payload}


@router.post("/api/v1/invoices/{token}/cancel")
async def api_cancel_invoice(
    token: str,
    context: ApiContext = Depends(api_context),
    session: AsyncSession = Depends(get_session),
):
    conditions = [Invoice.token == token, Invoice.merchant_id == context.merchant.id]
    if context.store and not context.legacy:
        conditions.append(Invoice.store_id == context.store.id)
    invoice = await session.scalar(select(Invoice).where(*conditions))
    if not invoice:
        raise HTTPException(status_code=404, detail={"code": "INVOICE_NOT_FOUND", "message": "فاکتور یافت نشد"})
    if invoice.status != "pending":
        raise HTTPException(status_code=409, detail={"code": "INVOICE_NOT_CANCELLABLE", "message": f"فاکتور در وضعیت {invoice.status} قابل لغو نیست", "details": {"status": invoice.status}})
    await release_invoice_reservation(session, invoice, "cancelled")
    await session.commit()
    payload = invoice_payload(invoice)
    if context.store:
        payload.update({"store_code": context.store.code, "store_name": context.store.name})
    return {"success": True, **payload}


@router.post("/api/v1/sandbox/invoices")
async def api_create_sandbox_invoice(
    request: Request,
    body: SandboxCreateInvoiceRequest,
    context: ApiContext = Depends(api_context),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
):
    if not context.store:
        raise HTTPException(status_code=400, detail={"code": "STORE_REQUIRED", "message": "Sandbox فقط برای کلید فروشگاهی فعال است"})
    if idempotency_key:
        idempotency_key = idempotency_key.strip()
        if not 8 <= len(idempotency_key) <= 180:
            raise HTTPException(status_code=400, detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "Idempotency-Key باید بین ۸ تا ۱۸۰ نویسه باشد"})
    request_hash = canonical_request_hash(body.model_dump(mode="json"))
    scope = f"sandbox:create_invoice:{context.store.id}"
    idem_row = None
    if idempotency_key:
        try:
            existing = await get_idempotent_response(session, scope=scope, key=idempotency_key, request_hash=request_hash)
        except ValueError as exc:
            code = str(exc)
            raise HTTPException(status_code=409, detail={"code": code, "message": "Idempotency-Key قبلاً استفاده شده یا در حال پردازش است"})
        if existing:
            status, response_payload = existing
            return JSONResponse(status_code=status, content=response_payload, headers={"Idempotency-Replayed": "true"})
        idem_row = await reserve_idempotency(session, scope=scope, key=idempotency_key, request_hash=request_hash)

    token = "test_" + secrets.token_urlsafe(18)
    invoice = SandboxInvoice(
        token=token,
        merchant_id=context.merchant.id,
        store_id=context.store.id,
        client_order_id=body.order_id,
        idempotency_key=idempotency_key,
        description=body.description,
        amount_rial=body.amount_toman * 10,
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes),
    )
    session.add(invoice)
    try:
        await session.flush()
        payload = {
            "success": True,
            "environment": "sandbox",
            "payment_id": invoice.token,
            "order_id": invoice.client_order_id,
            "status": invoice.status,
            "amount_rial": invoice.amount_rial,
            "payment_url": f"{settings.base_url}/sandbox/pay/{invoice.token}",
            "simulate_url": f"{settings.base_url}/api/v1/sandbox/invoices/{invoice.token}/simulate",
            "expires_at": invoice.expires_at.isoformat(),
            "store_id": context.store.id,
            "store_code": context.store.code,
        }
        await write_audit(
            session,
            action="sandbox.invoice.created",
            actor_type="api_key",
            actor_id=context.api_key.id if context.api_key else "legacy",
            merchant_id=context.merchant.id,
            store_id=context.store.id,
            entity_type="sandbox_invoice",
            entity_id=invoice.id,
            request_id=getattr(request.state, "request_id", None),
            ip_address=context.client_ip,
        )
        if idem_row:
            await finalize_idempotency(idem_row, status=200, response=payload, resource_type="sandbox_invoice", resource_id=invoice.id)
        await session.commit()
        return JSONResponse(content=payload, headers={"Idempotency-Key": idempotency_key} if idempotency_key else None)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail={"code": "SANDBOX_ORDER_CONFLICT", "message": "order_id یا Idempotency-Key قبلاً در Sandbox این فروشگاه استفاده شده است"})


@router.get("/api/v1/sandbox/invoices/{token}")
async def api_sandbox_invoice_status(
    token: str,
    context: ApiContext = Depends(api_context),
    session: AsyncSession = Depends(get_session),
):
    if not context.store:
        raise HTTPException(status_code=400, detail={"code": "STORE_REQUIRED", "message": "کلید فروشگاهی لازم است"})
    invoice = await session.scalar(select(SandboxInvoice).where(SandboxInvoice.token == token, SandboxInvoice.store_id == context.store.id))
    if not invoice:
        raise HTTPException(status_code=404, detail={"code": "SANDBOX_INVOICE_NOT_FOUND", "message": "فاکتور آزمایشی یافت نشد"})
    return {
        "success": True,
        "environment": "sandbox",
        "payment_id": invoice.token,
        "order_id": invoice.client_order_id,
        "status": invoice.status,
        "amount_rial": invoice.amount_rial,
        "reference_number": invoice.reference_number,
        "callback_status": invoice.callback_status,
        "expires_at": invoice.expires_at.isoformat(),
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
    }


@router.post("/api/v1/sandbox/invoices/{token}/simulate")
async def api_simulate_sandbox_invoice(
    request: Request,
    token: str,
    body: SandboxSimulationRequest,
    context: ApiContext = Depends(api_context),
    session: AsyncSession = Depends(get_session),
):
    if not context.store:
        raise HTTPException(status_code=400, detail={"code": "STORE_REQUIRED", "message": "کلید فروشگاهی لازم است"})
    invoice = await session.scalar(
        select(SandboxInvoice).where(SandboxInvoice.token == token, SandboxInvoice.store_id == context.store.id).with_for_update()
    )
    if not invoice:
        raise HTTPException(status_code=404, detail={"code": "SANDBOX_INVOICE_NOT_FOUND", "message": "فاکتور آزمایشی یافت نشد"})
    if invoice.status != "pending":
        raise HTTPException(status_code=409, detail={"code": "SANDBOX_INVOICE_FINAL", "message": f"فاکتور آزمایشی در وضعیت {invoice.status} است"})
    now = datetime.now(timezone.utc)
    if body.result == "paid":
        invoice.status = "paid"
        invoice.paid_at = now
        invoice.reference_number = body.reference_number or f"TEST-{secrets.token_hex(6).upper()}"
        await session.flush()
        await enqueue_sandbox_paid_callback(
            session,
            invoice,
            callback_url=context.store.callback_url,
            callback_secret=context.store.callback_secret,
        )
    elif body.result == "expired":
        invoice.status = "expired"
    else:
        invoice.status = "failed"
    await write_audit(
        session,
        action=f"sandbox.invoice.{invoice.status}",
        actor_type="api_key",
        actor_id=context.api_key.id if context.api_key else "legacy",
        merchant_id=context.merchant.id,
        store_id=context.store.id,
        entity_type="sandbox_invoice",
        entity_id=invoice.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=context.client_ip,
    )
    await session.commit()
    return {
        "success": True,
        "environment": "sandbox",
        "payment_id": invoice.token,
        "status": invoice.status,
        "reference_number": invoice.reference_number,
        "callback_status": invoice.callback_status,
    }


@router.get("/sandbox/pay/{token}", response_class=HTMLResponse, include_in_schema=False)
async def sandbox_payment_page(request: Request, token: str, session: AsyncSession = Depends(get_session)):
    invoice = await session.scalar(select(SandboxInvoice).where(SandboxInvoice.token == token))
    if not invoice:
        return templates.TemplateResponse("not_found.html", {"request": request}, status_code=404)
    return templates.TemplateResponse("sandbox.html", {"request": request, "invoice": invoice, "toman": rial_to_toman})


@router.get("/portal/{merchant_id}/{token}", response_class=HTMLResponse, include_in_schema=False)
async def merchant_portal(request: Request, merchant_id: int, token: str, session: AsyncSession = Depends(get_session)):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not verify_portal_token(merchant, token):
        return templates.TemplateResponse("not_found.html", {"request": request}, status_code=404)
    dashboard = await build_portal_dashboard(session, merchant)
    return templates.TemplateResponse(
        "portal.html",
        {"request": request, "merchant": merchant, "dashboard": dashboard, "toman": rial_to_toman, "app_version": APP_VERSION},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "X-Robots-Tag": "noindex, nofollow"},
    )


@router.post("/portal/{merchant_id}/{token}/stores/{store_id}/settings", include_in_schema=False)
async def portal_update_store_settings(
    request: Request,
    merchant_id: int,
    token: str,
    store_id: int,
    callback_url: str = Form(default=""),
    allowed_ips: str = Form(default=""),
    invoice_rate_limit_per_minute: int = Form(default=30),
    daily_amount_limit_toman: int = Form(default=500_000_000),
    session: AsyncSession = Depends(get_session),
):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not verify_portal_token(merchant, token):
        raise HTTPException(status_code=404, detail="Not found")
    store = await session.scalar(select(Store).where(Store.id == store_id, Store.merchant_id == merchant.id))
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    callback_value = callback_url.strip()
    if callback_value:
        valid, normalized = validate_public_https_url(callback_value)
        if not valid:
            raise HTTPException(status_code=422, detail=normalized)
        callback_value = normalized
    networks: list[str] = []
    for raw in allowed_ips.replace(";", ",").split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            networks.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"IP یا شبکه نامعتبر است: {item}")
    store.callback_url = callback_value or None
    store.allowed_ips = ",".join(networks) or None
    store.invoice_rate_limit_per_minute = max(1, min(300, invoice_rate_limit_per_minute))
    store.daily_amount_limit_rial = max(1_000, min(5_000_000_000, daily_amount_limit_toman)) * 10
    await write_audit(
        session,
        action="portal.store.settings.updated",
        actor_type="merchant_portal",
        actor_id=merchant.telegram_user_id,
        merchant_id=merchant.id,
        store_id=store.id,
        entity_type="store",
        entity_id=store.id,
        request_id=getattr(request.state, "request_id", None),
        ip_address=request.client.host if request.client else None,
    )
    await session.commit()
    return RedirectResponse(url=f"/portal/{merchant_id}/{token}#stores", status_code=303)


@router.post("/portal/{merchant_id}/{token}/reconciliation/{case_id}/resolve", include_in_schema=False)
async def portal_resolve_case(
    request: Request,
    merchant_id: int,
    token: str,
    case_id: int,
    session: AsyncSession = Depends(get_session),
):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not verify_portal_token(merchant, token):
        raise HTTPException(status_code=404, detail="Not found")
    from app.models import ReconciliationCase
    row = await session.scalar(select(ReconciliationCase).where(ReconciliationCase.id == case_id, ReconciliationCase.merchant_id == merchant.id))
    if row:
        row.status = "resolved"
        row.resolution = "بسته‌شده از پنل وب پذیرنده"
        row.resolved_by = str(merchant.telegram_user_id)
        row.resolved_at = datetime.now(timezone.utc)
        await write_audit(session, action="reconciliation.resolved", actor_type="merchant_portal", actor_id=merchant.telegram_user_id, merchant_id=merchant.id, entity_type="reconciliation_case", entity_id=row.id, request_id=getattr(request.state, "request_id", None))
        await session.commit()
    return RedirectResponse(url=f"/portal/{merchant_id}/{token}#reconciliation", status_code=303)


@router.post("/portal/{merchant_id}/{token}/callbacks/{event_id}/retry", include_in_schema=False)
async def portal_retry_callback(
    request: Request,
    merchant_id: int,
    token: str,
    event_id: int,
    session: AsyncSession = Depends(get_session),
):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not verify_portal_token(merchant, token):
        raise HTTPException(status_code=404, detail="Not found")
    from app.models import CallbackEvent
    row = await session.scalar(select(CallbackEvent).where(CallbackEvent.id == event_id, CallbackEvent.merchant_id == merchant.id))
    if row and row.status == "failed":
        row.status = "retry"
        row.attempt_count = 0
        row.next_attempt_at = datetime.now(timezone.utc)
        row.locked_at = None
        row.last_result = "portal_retry_requested"
        await write_audit(session, action="callback.retry_requested", actor_type="merchant_portal", actor_id=merchant.telegram_user_id, merchant_id=merchant.id, store_id=row.store_id, entity_type="callback_event", entity_id=row.id, request_id=getattr(request.state, "request_id", None))
        await session.commit()
    return RedirectResponse(url=f"/portal/{merchant_id}/{token}#callbacks", status_code=303)


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def metrics(session: AsyncSession = Depends(get_session)):
    return PlainTextResponse(await prometheus_metrics(session), media_type="text/plain; version=0.0.4")


@router.get("/api/payment/{token}/status")
async def public_invoice_status(token: str, session: AsyncSession = Depends(get_session)):
    invoice = await session.scalar(select(Invoice).where(Invoice.token == token))
    if not invoice:
        raise HTTPException(status_code=404, detail="فاکتور یافت نشد")
    now = datetime.now(timezone.utc)
    expires_at = invoice.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if invoice.status == "pending" and expires_at < now:
        await release_invoice_reservation(session, invoice, "expired")
        await session.commit()
    return {"status": invoice.status, "reference_number": invoice.reference_number}


@router.get("/pay/{token}", response_class=HTMLResponse)
async def payment_page(request: Request, token: str, session: AsyncSession = Depends(get_session)):
    invoice = await get_invoice_by_token(session, token)
    if not invoice:
        return templates.TemplateResponse("not_found.html", {"request": request}, status_code=404)

    now = datetime.now(timezone.utc)
    expires_at = invoice.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if invoice.status == "pending" and expires_at < now:
        await release_invoice_reservation(session, invoice, "expired")
        await session.commit()

    if invoice.status == "paid":
        return templates.TemplateResponse("success.html", {"request": request, "invoice": invoice, "toman": rial_to_toman})
    if invoice.status in {"expired", "cancelled"}:
        return templates.TemplateResponse("expired.html", {"request": request, "invoice": invoice, "toman": rial_to_toman})

    encryption_key = await get_setting(session, "encryption_key")
    try:
        card_number = decrypt_text(invoice.card.card_number_encrypted, encryption_key or "")
        card_display = "-".join(card_number[i:i+4] for i in range(0, 16, 4))
        card_copy = card_number
    except Exception:
        card_display = "****-****-****-" + invoice.card.card_last4
        card_copy = ""
    return templates.TemplateResponse(
        "payment.html",
        {
            "request": request,
            "invoice": invoice,
            "card_display": card_display,
            "card_copy": card_copy,
            "toman": rial_to_toman,
            "bank_name": bank_label(invoice.card.bank_code),
        },
    )


@router.post("/webhooks/sms/{merchant_id}/{token}")
async def merchant_sms_webhook(
    merchant_id: int,
    token: str,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not merchant.is_active or not merchant.callback_secret:
        raise HTTPException(status_code=404, detail={"code": "SMS_WEBHOOK_NOT_FOUND", "message": "Webhook یافت نشد"})
    if not hmac.compare_digest(token, merchant_sms_token(merchant)):
        raise HTTPException(status_code=401, detail={"code": "INVALID_SMS_WEBHOOK_TOKEN", "message": "Webhook token نامعتبر است"})

    try:
        sender, message, device_id, bank_code = await _read_sms_webhook_payload(request)
    except SmsPayloadError as exc:
        background_tasks.add_task(
            send_invalid_sms_payload_notice,
            merchant,
            exc.code,
            exc.detail,
            exc.preview,
        )
        return error_response(
            request,
            status_code=422,
            code=exc.code,
            message=exc.detail,
            field="message",
            details={"preview": exc.preview[:240] if exc.preview else None},
        )

    trusted_devices_raw = await get_setting(session, f"sms_trusted_devices:{merchant.id}")
    if trusted_devices_raw:
        trusted_devices = {item.strip().casefold() for item in trusted_devices_raw.replace(";", ",").split(",") if item.strip()}
        incoming_device = (device_id or "").strip().casefold()
        if trusted_devices and incoming_device not in trusted_devices:
            await write_audit(
                session,
                action="sms.device_blocked",
                actor_type="webhook",
                actor_id=device_id or "unknown",
                merchant_id=merchant.id,
                entity_type="sms_device",
                entity_id=device_id or "unknown",
                request_id=getattr(request.state, "request_id", None),
                metadata={"trusted_devices_count": len(trusted_devices)},
            )
            await session.commit()
            raise HTTPException(status_code=403, detail={"code": "SMS_DEVICE_NOT_TRUSTED", "message": "این دستگاه در فهرست مجاز پیامک پذیرنده نیست"})

    sms, invoice, diagnostic = await ingest_sms(
        session,
        sender,
        message,
        device_id,
        merchant_id=merchant.id,
        bank_hint=bank_code,
    )
    if diagnostic.result in {"amount_not_found", "low_confidence", "no_amount_candidate", "bank_or_card_mismatch", "source_mismatch_ambiguous", "ambiguous", "race_or_already_paid"}:
        await open_reconciliation_case(
            session,
            case_key=f"sms:{sms.id}:{diagnostic.result}",
            case_type=diagnostic.result,
            detail=diagnostic.detail,
            merchant_id=merchant.id,
            sms_id=sms.id,
            severity="high" if diagnostic.result in {"ambiguous", "race_or_already_paid"} else "medium",
        )
    await write_audit(
        session,
        action=f"sms.{diagnostic.result}",
        actor_type="webhook",
        actor_id=device_id or sender,
        merchant_id=merchant.id,
        entity_type="sms_transaction",
        entity_id=sms.id,
        request_id=getattr(request.state, "request_id", None),
        metadata={"bank": sms.bank_code, "amount_rial": sms.amount_rial, "confidence": sms.parse_confidence},
    )
    await session.commit()
    if diagnostic.notify:
        background_tasks.add_task(send_sms_processing_notice, merchant, sms, invoice, diagnostic)
    if invoice:
        background_tasks.add_task(send_paid_callback, invoice)
    return {
        "success": True,
        "result": diagnostic.result,
        "detail": diagnostic.detail,
        "sms_id": sms.id,
        "sms_status": sms.status,
        "detected_bank": sms.bank_code,
        "detected_amount_rial": sms.amount_rial,
        "detected_card_last4": sms.card_last4,
        "parse_confidence": sms.parse_confidence,
        "invoice_id": invoice.token if invoice else None,
        "amount_candidate_count": diagnostic.amount_candidate_count,
        "bank_candidate_count": diagnostic.bank_candidate_count,
        "source_candidate_count": diagnostic.source_candidate_count,
        "gateway_version": APP_VERSION,
        "received_sender": sender[:120],
        "received_message_preview": message[:240],
        "notification_sent": diagnostic.notify,
    }


@router.post("/webhooks/sms", deprecated=True)
async def legacy_sms_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_sms_secret: str = Header(default="", alias="X-SMS-Secret"),
    session: AsyncSession = Depends(get_session),
):
    """Legacy system-wide endpoint kept for existing installations.

    New merchants must use their own tokenized webhook URL shown in the bot.
    """
    expected = await get_setting(session, "sms_webhook_secret")
    if not expected or not hmac.compare_digest(x_sms_secret, expected):
        raise HTTPException(status_code=401, detail={"code": "INVALID_SMS_WEBHOOK_SECRET", "message": "Webhook secret نامعتبر است"})

    try:
        sender, message, device_id, bank_code = await _read_sms_webhook_payload(request)
    except SmsPayloadError as exc:
        return error_response(
            request,
            status_code=422,
            code=exc.code,
            message=exc.detail,
            field="message",
            details={"preview": exc.preview[:240] if exc.preview else None},
        )
    sms, invoice, diagnostic = await ingest_sms(session, sender, message, device_id, bank_hint=bank_code)
    await session.commit()
    if invoice:
        background_tasks.add_task(send_paid_callback, invoice)
    return {
        "success": True,
        "result": diagnostic.result,
        "detail": diagnostic.detail,
        "sms_id": sms.id,
        "invoice_id": invoice.token if invoice else None,
        "deprecated": True,
    }
