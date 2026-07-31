from __future__ import annotations

import hmac
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import api_merchant
from app.api.schemas import CreateInvoiceRequest
from app.core.config import settings
from app.core.security import decrypt_text
from app.core.urls import validate_public_https_url
from app.db.session import get_session
from app.models import Invoice, Merchant
from app.parsers import BANK_PROFILES, bank_label
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
from app.services.sms_notification_service import send_invalid_sms_payload_notice, send_sms_processing_notice
from app.services.storage_service import storage

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def rial_to_toman(value: int) -> int:
    return value // 10


class SmsPayloadError(ValueError):
    def __init__(self, code: str, detail: str, preview: str = "") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.preview = preview[:500]


_UNRESOLVED_PLACEHOLDERS = (
    "{incoming number}",
    "{{incoming number}}",
    "{message body}",
    "{{message body}}",
    "<incoming number>",
    "<message body>",
)


def _is_unresolved_placeholder(value: str | None) -> bool:
    if not value:
        return False
    normalized = " ".join(value.strip().casefold().split())
    return any(token in normalized for token in _UNRESOLVED_PLACEHOLDERS)


def _split_combined_forwarded_message(value: str | None) -> tuple[str | None, str | None]:
    """Split the default/custom Zerogic forwarded text when it contains From:.

    A common Message Template is:
      From : +989... (Blu Bank)
      <original SMS body>
    """
    if not value:
        return None, None
    match = re.match(
        r"(?is)^\s*(?:FROM|SENDER|فرستنده)\s*:\s*(?P<sender>[^\r\n]+)[\r\n]+(?P<message>.+)$",
        value.strip(),
    )
    if not match:
        return None, None
    return match.group("sender").strip(), match.group("message").strip()


def _first_payload_value(data: dict, *keys: str, skip_placeholders: bool = False):
    for key in keys:
        value = data.get(key)
        if value is None or not str(value).strip():
            continue
        normalized = str(value).strip()
        if skip_placeholders and _is_unresolved_placeholder(normalized):
            continue
        return normalized
    return None


def _parse_text_sms_payload(raw: str) -> dict:
    text = raw.strip()
    if not text:
        return {}

    # JSON sent as text/plain is accepted too.
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except Exception:
        pass

    # Legacy text template is still accepted for backward compatibility.
    # Recommended SMS Forwarder JSON body:
    # Dynamic fields must be inserted by the forwarding app, not typed as literal braces.
    marker = re.match(
        r"(?is)^\s*DEVICE\s*:\s*(?P<device>[^\r\n]*)[\r\n]+"
        r"\s*SENDER\s*:\s*(?P<sender>[^\r\n]*)[\r\n]+"
        r"\s*MESSAGE\s*:\s*[\r\n]*(?P<message>.*)$",
        text,
    )
    if marker:
        return {
            "device_id": marker.group("device").strip(),
            "sender": marker.group("sender").strip(),
            "message": marker.group("message").strip(),
        }

    # Also accept From:/Sender: followed by the original message body.
    simple = re.match(
        r"(?is)^\s*(?:FROM|SENDER|فرستنده)\s*:\s*(?P<sender>[^\r\n]*)[\r\n]+(?P<message>.*)$",
        text,
    )
    if simple:
        return {
            "sender": simple.group("sender").strip(),
            "message": simple.group("message").strip(),
        }

    return {"message": text}


async def _read_sms_webhook_payload(request: Request) -> tuple[str, str, str | None, str | None]:
    content_type = (request.headers.get("content-type") or "").lower()
    data: dict = {}

    raw_body = await request.body()
    raw_text = raw_body.decode("utf-8", errors="replace")
    try:
        if "application/json" in content_type:
            try:
                value = json.loads(raw_text)
                if isinstance(value, dict):
                    data = value
            except Exception:
                # Some apps label a custom text template as application/json.
                data = _parse_text_sms_payload(raw_text)
        elif "application/x-www-form-urlencoded" in content_type:
            from urllib.parse import parse_qs

            parsed = parse_qs(raw_text, keep_blank_values=True)
            data = {key: values[-1] if values else "" for key, values in parsed.items()}
        else:
            data = _parse_text_sms_payload(raw_text)
    except Exception:
        data = _parse_text_sms_payload(raw_text)

    # Some forwarders wrap the actual values in data/payload/messageData.
    for wrapper in ("data", "payload", "messageData", "sms", "smsData", "notification", "event"):
        nested = data.get(wrapper)
        if isinstance(nested, dict):
            data = {**data, **nested}

    # Query parameters can fill missing fields.
    for key, value in request.query_params.items():
        data.setdefault(key, value)

    sender_keys = (
        "sender", "from", "source", "address", "incoming_number", "incomingNumber",
        "incoming", "number", "phone", "fromNumber", "from_number", "phoneNumber",
        "originator", "originalSender", "original_sender", "title", "senderName",
        "sender_name", "contactName", "contact_name",
    )
    message_keys = (
        "message", "body", "text", "msg", "content", "message_body", "messageBody",
        "sms_body", "smsBody", "originalBody", "original_body", "originalMessage",
        "original_message", "transformedBody", "transformed_body", "description",
    )
    supplied_sender = _first_payload_value(data, *sender_keys)
    supplied_message = _first_payload_value(data, *message_keys)
    had_unresolved_template = (
        _is_unresolved_placeholder(supplied_sender) or _is_unresolved_placeholder(supplied_message)
    )
    # Prefer actual fields shipped by the app over a custom field that still
    # contains a literal placeholder copied by the user.
    sender = _first_payload_value(data, *sender_keys, skip_placeholders=True)
    message = _first_payload_value(data, *message_keys, skip_placeholders=True)
    device_id = _first_payload_value(
        data, "device_id", "deviceId", "device", "phone_id", "phoneId", "sim", "sim_id",
        "subscriptionId", "deviceModel", "device_name", "deviceName",
    )
    bank_code = _first_payload_value(data, "bank_code", "bankCode", "bank")

    # If the app sends the complete customized template as one field, recover
    # sender and original SMS body from the leading From:/Sender: line.
    combined_sender, combined_message = _split_combined_forwarded_message(message)
    if combined_sender and combined_message:
        if not sender or _is_unresolved_placeholder(sender):
            sender = combined_sender
        message = combined_message

    # Older/default text mode can arrive as the entire raw body, even when the
    # selected content-type is inaccurate. Try that form before rejecting it.
    if (not sender or not message or _is_unresolved_placeholder(sender) or _is_unresolved_placeholder(message)):
        raw_sender, raw_message = _split_combined_forwarded_message(raw_text)
        if raw_sender and raw_message:
            sender, message = raw_sender, raw_message

    unresolved_sender = _is_unresolved_placeholder(sender)
    unresolved_message = _is_unresolved_placeholder(message)
    if unresolved_sender or unresolved_message or (had_unresolved_template and (not sender or not message)):
        raise SmsPayloadError(
            "SMS_TEMPLATE_NOT_RESOLVED",
            "متغیرهای SMS Forwarder جایگزین نشده‌اند. Incoming Number و Message Body را از بخش Message Template خود برنامه به‌صورت فیلد پویا اضافه کن؛ تایپ یا کپی ساده عبارت‌های داخل آکولاد کافی نیست.",
            raw_text,
        )

    if not sender or len(sender) > 120:
        raise SmsPayloadError(
            "SMS_SENDER_MISSING",
            "شماره/نام فرستنده واقعی پیامک در درخواست موجود نیست",
            raw_text,
        )
    if not message or len(message) < 3 or len(message) > 4000:
        raise SmsPayloadError(
            "SMS_MESSAGE_MISSING",
            "متن واقعی پیامک در درخواست موجود نیست یا طول آن نامعتبر است",
            raw_text,
        )
    if device_id and len(device_id) > 120:
        raise SmsPayloadError("SMS_DEVICE_TOO_LONG", "device_id بیش از حد طولانی است", raw_text)
    if bank_code and len(bank_code) > 60:
        raise SmsPayloadError("SMS_BANK_CODE_TOO_LONG", "bank_code بیش از حد طولانی است", raw_text)

    return sender, message, device_id, bank_code


def invoice_payload(invoice: Invoice) -> dict:
    return {
        "payment_id": invoice.token,
        "order_id": invoice.order_id,
        "status": invoice.status,
        "base_amount_rial": invoice.base_amount_rial,
        "fee_amount_rial": invoice.fee_amount_rial,
        "customer_fee_rial": invoice.customer_fee_rial,
        "unique_amount_rial": invoice.unique_amount_rial,
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
        "version": "0.3.3",
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
            "banks": BANK_PROFILES,
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
            "banks": BANK_PROFILES,
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
        raise HTTPException(status_code=404, detail="Webhook یافت نشد")
    if not hmac.compare_digest(token, merchant_sms_token(merchant)):
        raise HTTPException(status_code=401, detail="Webhook token نامعتبر است")

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
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": exc.code,
                "detail": exc.detail,
            },
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
        raise HTTPException(status_code=401, detail="Webhook secret نامعتبر است")

    try:
        sender, message, device_id, bank_code = await _read_sms_webhook_payload(request)
    except SmsPayloadError as exc:
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": exc.code, "detail": exc.detail},
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
