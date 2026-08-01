from __future__ import annotations

import hmac
import httpx
import json
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiContext, api_context
from app.api.errors import error_response
from app.api.schemas import CreateInvoiceRequest
from app.core.config import settings
from app.core.security import decrypt_text
from app.core.urls import validate_public_https_url
from app.db.session import get_session
from app.models import Invoice, Merchant, Store
from app.parsers import BANK_PROFILES, bank_label
from app.services.callback_service import send_paid_callback
from app.services.integration_service import (
    merchant_docs_url,
    merchant_sms_token,
    merchant_sms_webhook_url,
)
from app.services.invoice_service import create_invoice, get_invoice_by_token, release_invoice_reservation
from app.services.settings_service import get_setting
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
        "expires_at": invoice.expires_at.isoformat(),
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "reference_number": invoice.reference_number,
        "store_id": invoice.store_id,
        "api_key_id": invoice.api_key_id,
    }


@router.get("/health")
async def health():
    backup = storage.status()
    return {
        "ok": True,
        "storage_ok": backup["last_error"] is None,
        "service": "gateway-bot",
        "version": APP_VERSION,
        "database": "auto-sqlite-encrypted-github",
        "backup": backup,
    }


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "base_url": settings.base_url,
            "docs_url": f"{settings.base_url}/developers",
            "telegram_url": f"{settings.base_url}/telegram",
            "health_url": f"{settings.base_url}/health",
            "app_version": APP_VERSION,
        },
        headers={"Cache-Control": "public, max-age=300"},
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
            "docs_contract_version": "2026-08-01.3",
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


@router.post("/api/v1/invoices")
async def api_create_invoice(
    body: CreateInvoiceRequest,
    context: ApiContext = Depends(api_context),
    session: AsyncSession = Depends(get_session),
):
    merchant = context.merchant
    try:
        callback_url = str(body.callback_url) if body.callback_url else None
        if callback_url:
            valid, normalized = validate_public_https_url(callback_url)
            if not valid:
                raise ValueError(normalized)
            callback_url = normalized
        invoice = await create_invoice(
            session,
            merchant=merchant,
            base_amount_rial=body.amount_toman * 10,
            description=body.description,
            order_id=(
                f"API-{context.store.id}-{secrets.token_hex(8).upper()}"
                if context.store and body.order_id
                else body.order_id
            ),
            client_order_id=body.order_id if context.store else None,
            fee_mode=body.fee_mode,
            card_id=body.card_id,
            callback_url=(
                callback_url
                or (context.store.callback_url if context.store else merchant.callback_url)
            ),
            callback_secret=(
                context.store.callback_secret
                if context.store and (callback_url or context.store.callback_url)
                else (merchant.callback_secret if not context.store else None)
            ),
            ttl_minutes=body.ttl_minutes,
            store_id=context.store.id if context.store else None,
            api_key_id=context.api_key.id if context.api_key else None,
        )
        await session.commit()
        payload = invoice_payload(invoice)
        if context.store:
            payload["store_code"] = context.store.code
            payload["store_name"] = context.store.name
        return {"success": True, **payload}
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail={"code": "ORDER_ID_CONFLICT", "message": "order_id قبلاً در این فروشگاه استفاده شده است", "field": "order_id"})
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

    sms, invoice, diagnostic = await ingest_sms(
        session,
        sender,
        message,
        device_id,
        merchant_id=merchant.id,
        bank_hint=bank_code,
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
