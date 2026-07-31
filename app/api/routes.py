from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import api_merchant
from app.api.schemas import CreateInvoiceRequest, SmsWebhookRequest
from app.core.config import settings
from app.core.security import decrypt_text
from app.core.urls import validate_public_https_url
from app.db.session import get_session
from app.models import Invoice, Merchant
from app.services.callback_service import send_paid_callback
from app.services.integration_service import (
    merchant_docs_token,
    merchant_docs_url,
    merchant_sms_token,
    merchant_sms_webhook_url,
)
from app.services.invoice_service import create_invoice, get_invoice_by_token, release_invoice_reservation
from app.services.settings_service import get_setting
from app.services.sms_service import ingest_sms
from app.services.storage_service import storage

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def rial_to_toman(value: int) -> int:
    return value // 10


def invoice_payload(invoice: Invoice) -> dict:
    return {
        "payment_id": invoice.token,
        "order_id": invoice.order_id,
        "status": invoice.status,
        "base_amount_rial": invoice.base_amount_rial,
        "fee_amount_rial": invoice.fee_amount_rial,
        "customer_fee_rial": invoice.customer_fee_rial,
        "payable_amount_rial": invoice.payable_amount_rial,
        "payment_url": f"{settings.base_url}/pay/{invoice.token}",
        "expires_at": invoice.expires_at.isoformat(),
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "reference_number": invoice.reference_number,
    }


@router.get("/health")
async def health():
    backup = storage.status()
    return {
        "ok": True,
        "storage_ok": backup["last_error"] is None,
        "service": "gateway-bot",
        "version": "0.2.6",
        "database": "auto-sqlite-encrypted-github",
        "backup": backup,
    }


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("home.html", {"request": request, "docs_url": f"{settings.base_url}/developers"})


@router.get("/developers", response_class=HTMLResponse)
async def developer_docs(request: Request):
    return templates.TemplateResponse(
        "developers.html",
        {
            "request": request,
            "base_url": settings.base_url,
            "merchant": None,
            "sms_webhook_url": f"{settings.base_url}/webhooks/sms/MERCHANT_ID/UNIQUE_TOKEN",
            "api_prefix": "gw_...",
            "callback_url": "https://example.com/bluepay/webhook",
        },
    )


@router.get("/developers/{merchant_id}/{token}", response_class=HTMLResponse)
async def personalized_developer_docs(
    request: Request,
    merchant_id: int,
    token: str,
    session: AsyncSession = Depends(get_session),
):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not merchant.is_active or not merchant.callback_secret:
        raise HTTPException(status_code=404, detail="مستندات پذیرنده یافت نشد")
    if not hmac.compare_digest(token, merchant_docs_token(merchant)):
        raise HTTPException(status_code=404, detail="مستندات پذیرنده یافت نشد")
    return templates.TemplateResponse(
        "developers.html",
        {
            "request": request,
            "base_url": settings.base_url,
            "merchant": merchant,
            "sms_webhook_url": merchant_sms_webhook_url(merchant),
            "api_prefix": merchant.api_key_prefix or "هنوز ساخته نشده",
            "callback_url": merchant.callback_url or "هنوز تنظیم نشده",
        },
    )


@router.get("/api/v1/account")
async def api_account(merchant: Merchant = Depends(api_merchant)):
    return {
        "success": True,
        "merchant_id": merchant.id,
        "name": merchant.name,
        "fee_mode": merchant.fee_mode,
        "verification_fee_rial": merchant.verification_fee_rial,
        "available_balance_rial": merchant.available_balance_rial,
        "callback_configured": bool(merchant.callback_url),
        "sms_webhook_url": merchant_sms_webhook_url(merchant),
        "developer_docs_url": merchant_docs_url(merchant),
    }


@router.post("/api/v1/invoices")
async def api_create_invoice(
    body: CreateInvoiceRequest,
    merchant: Merchant = Depends(api_merchant),
    session: AsyncSession = Depends(get_session),
):
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
            order_id=body.order_id,
            fee_mode=body.fee_mode,
            card_id=body.card_id,
            callback_url=callback_url,
            ttl_minutes=body.ttl_minutes,
        )
        await session.commit()
        return {"success": True, **invoice_payload(invoice)}
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="order_id قبلاً استفاده شده است")
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/v1/invoices/{token}")
async def api_invoice_status(
    token: str,
    merchant: Merchant = Depends(api_merchant),
    session: AsyncSession = Depends(get_session),
):
    invoice = await session.scalar(select(Invoice).where(Invoice.token == token, Invoice.merchant_id == merchant.id))
    if not invoice:
        raise HTTPException(status_code=404, detail="فاکتور یافت نشد")
    return {"success": True, **invoice_payload(invoice)}


@router.post("/api/v1/invoices/{token}/cancel")
async def api_cancel_invoice(
    token: str,
    merchant: Merchant = Depends(api_merchant),
    session: AsyncSession = Depends(get_session),
):
    invoice = await session.scalar(select(Invoice).where(Invoice.token == token, Invoice.merchant_id == merchant.id))
    if not invoice:
        raise HTTPException(status_code=404, detail="فاکتور یافت نشد")
    if invoice.status != "pending":
        raise HTTPException(status_code=409, detail=f"فاکتور در وضعیت {invoice.status} قابل لغو نیست")
    await release_invoice_reservation(session, invoice, "cancelled")
    await session.commit()
    return {"success": True, **invoice_payload(invoice)}


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
        },
    )


@router.post("/webhooks/sms/{merchant_id}/{token}")
async def merchant_sms_webhook(
    merchant_id: int,
    token: str,
    body: SmsWebhookRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not merchant.is_active or not merchant.callback_secret:
        raise HTTPException(status_code=404, detail="Webhook یافت نشد")
    if not hmac.compare_digest(token, merchant_sms_token(merchant)):
        raise HTTPException(status_code=401, detail="Webhook token نامعتبر است")

    sms, invoice, result = await ingest_sms(
        session,
        body.sender,
        body.message,
        body.device_id,
        merchant_id=merchant.id,
    )
    await session.commit()
    if invoice:
        background_tasks.add_task(send_paid_callback, invoice)
    return {
        "success": True,
        "result": result,
        "sms_id": sms.id,
        "invoice_id": invoice.token if invoice else None,
    }


@router.post("/webhooks/sms", deprecated=True)
async def legacy_sms_webhook(
    body: SmsWebhookRequest,
    background_tasks: BackgroundTasks,
    x_sms_secret: str = Header(default="", alias="X-SMS-Secret"),
    session: AsyncSession = Depends(get_session),
):
    """Legacy system-wide endpoint kept for existing installations.

    New merchants must use their own tokenized webhook URL shown in the bot.
    """
    expected = await get_setting(session, "sms_webhook_secret")
    if not expected or not hmac.compare_digest(x_sms_secret, expected):
        raise HTTPException(status_code=401, detail="Webhook secret نامعتبر است")

    sms, invoice, result = await ingest_sms(session, body.sender, body.message, body.device_id)
    await session.commit()
    if invoice:
        background_tasks.add_task(send_paid_callback, invoice)
    return {
        "success": True,
        "result": result,
        "sms_id": sms.id,
        "invoice_id": invoice.token if invoice else None,
        "deprecated": True,
    }
