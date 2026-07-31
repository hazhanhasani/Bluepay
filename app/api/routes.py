from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.deps import api_merchant
from app.api.schemas import CreateInvoiceRequest, SmsWebhookRequest
from app.core.config import settings
from app.core.security import decrypt_text
from app.db.session import get_session
from app.models import Invoice, Merchant
from app.services.callback_service import send_paid_callback
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
        "ok": backup["last_error"] is None,
        "service": "gateway-bot",
        "version": "0.2.0",
        "database": "auto-sqlite-encrypted-github",
        "backup": backup,
    }


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("home.html", {"request": request})


@router.post("/api/v1/invoices")
async def api_create_invoice(
    body: CreateInvoiceRequest,
    merchant: Merchant = Depends(api_merchant),
    session: AsyncSession = Depends(get_session),
):
    try:
        invoice = await create_invoice(
            session,
            merchant=merchant,
            base_amount_rial=body.amount_toman * 10,
            description=body.description,
            order_id=body.order_id,
            fee_mode=body.fee_mode,
            card_id=body.card_id,
            callback_url=str(body.callback_url) if body.callback_url else None,
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


@router.post("/webhooks/sms")
async def sms_webhook(
    body: SmsWebhookRequest,
    background_tasks: BackgroundTasks,
    x_sms_secret: str = Header(default="", alias="X-SMS-Secret"),
    session: AsyncSession = Depends(get_session),
):
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
    }
