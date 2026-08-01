from __future__ import annotations

import io
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ApiContext, api_context
from app.core.config import settings
from app.core.urls import validate_public_https_url
from app.db.session import get_session
from app.models import (
    AbExperiment,
    AbVariant,
    AdminInboxItem,
    AutomationRule,
    Branch,
    CashierShift,
    Campaign,
    CardRoutingRule,
    Customer,
    CustomerWallet,
    CustomerWalletEntry,
    FraudRule,
    FulfillmentJob,
    IntegrationConnector,
    Invoice,
    Merchant,
    MerchantOptionProfile,
    MerchantVerification,
    MessageTemplate,
    PartialPayment,
    PaymentLink,
    Product,
    RefundRequest,
    SmsDevice,
    SmsParserTemplate,
    Affiliate,
    AffiliateCommission,
    DiscountCode,
    InvoiceTemplate,
    PaymentReminder,
    PaymentRequest,
    ScheduledInvoice,
    Subscription,
    SubscriptionPlan,
    SupportMessage,
    SupportTicket,
    WebhookSubscription,
)
from app.services.options_service import (
    analytics_funnel,
    create_customer,
    create_invoice_from_payment_link,
    create_payment_link,
    create_refund_request,
    customer_portal_dashboard,
    encrypt_private_config,
    get_customer_by_portal_token,
    json_dumps,
    json_loads,
    ensure_option_profile,
    option_summary,
    record_analytics_event,
    record_partial_payment,
    rotate_customer_portal_token,
    select_ab_variant,
    create_invoice_from_template,
    dispatch_payment_request,
    resolve_affiliate_code,
    resolve_discount_code,
    slugify,
)
from app.services.portal_service import verify_portal_token
from app.services.invoice_service import create_invoice
from app.services.integration_service import merchant_sms_token
from app.services.sms_device_service import authenticate_sms_device
from app.services.sms_service import ingest_sms
from app.services.settings_service import get_setting
from app.version import APP_VERSION

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

RESOURCE_MODELS = {
    "products": Product,
    "customers": Customer,
    "payment-links": PaymentLink,
    "automation-rules": AutomationRule,
    "connectors": IntegrationConnector,
    "campaigns": Campaign,
    "branches": Branch,
    "message-templates": MessageTemplate,
    "webhook-subscriptions": WebhookSubscription,
    "card-routing-rules": CardRoutingRule,
    "fraud-rules": FraudRule,
    "sms-parser-templates": SmsParserTemplate,
    "refunds": RefundRequest,
    "ab-experiments": AbExperiment,
    "ab-variants": AbVariant,
    "admin-inbox": AdminInboxItem,
    "fulfillment-jobs": FulfillmentJob,
    "invoice-templates": InvoiceTemplate,
    "subscription-plans": SubscriptionPlan,
    "subscriptions": Subscription,
    "discount-codes": DiscountCode,
    "affiliates": Affiliate,
    "affiliate-commissions": AffiliateCommission,
    "support-tickets": SupportTicket,
    "payment-reminders": PaymentReminder,
    "payment-requests": PaymentRequest,
    "scheduled-invoices": ScheduledInvoice,
}

SENSITIVE_FIELDS = {
    "phone_encrypted", "email_encrypted", "portal_token_hash", "config_encrypted",
    "secret_encrypted", "destination_encrypted", "fulfillment_config_encrypted",
}

READ_ONLY_RESOURCES = {"affiliate-commissions", "payment-reminders", "ab-variants", "fulfillment-jobs"}
SYSTEM_MANAGED_FIELDS = {
    "used_count", "wallet_balance_rial", "run_count", "last_run_at", "tested_count",
    "current_cycle", "last_invoice_id", "invoice_id", "attempt_count", "last_error",
    "created_at", "updated_at", "merchant_id", "approved_at", "paid_at",
}


def _serialize(row: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in row.__table__.columns:
        name = column.name
        if name in SENSITIVE_FIELDS:
            result[name.replace("_encrypted", "_configured").replace("_hash", "_configured")] = bool(getattr(row, name, None))
            continue
        value = getattr(row, name, None)
        result[name] = value.isoformat() if isinstance(value, datetime) else value
    return result


def _scope_stmt(model: Any, context: ApiContext):
    if model is AbVariant:
        stmt = select(AbVariant).join(AbExperiment, AbExperiment.id == AbVariant.experiment_id).where(AbExperiment.merchant_id == context.merchant.id)
        if context.store:
            stmt = stmt.where((AbExperiment.store_id == context.store.id) | (AbExperiment.store_id.is_(None)))
        return stmt
    stmt = select(model).where(model.merchant_id == context.merchant.id)
    if context.store and hasattr(model, "store_id"):
        stmt = stmt.where((model.store_id == context.store.id) | (model.store_id.is_(None)))
    return stmt


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_JSON", "message": "بدنه JSON معتبر نیست"}) from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail={"code": "INVALID_BODY", "message": "بدنه درخواست باید object باشد"})
    return value




def _parse_datetime(value: Any, *, default: datetime | None = None) -> datetime | None:
    if value in (None, ""):
        return default
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


async def _create_resource(session: AsyncSession, context: ApiContext, resource: str, data: dict[str, Any]):
    merchant_id = context.merchant.id
    store_id = context.store.id if context.store else data.get("store_id")
    if resource == "products":
        price = int(data.get("price_rial") or int(data.get("price_toman") or 0) * 10)
        if price < 10_000:
            raise ValueError("قیمت محصول باید حداقل ۱٬۰۰۰ تومان باشد")
        row = Product(
            merchant_id=merchant_id,
            store_id=store_id,
            name=str(data.get("name") or "محصول")[:160],
            slug=slugify(str(data.get("slug") or data.get("name") or "product"), "product"),
            description=data.get("description"),
            price_rial=price,
            inventory_count=int(data["inventory_count"]) if data.get("inventory_count") not in (None, "") else None,
            image_url=data.get("image_url"),
            fulfillment_type=str(data.get("fulfillment_type") or "manual")[:40],
            fulfillment_config_encrypted=await encrypt_private_config(session, data.get("fulfillment_config")),
        )
    elif resource == "customers":
        row, token = await create_customer(
            session,
            merchant_id,
            name=str(data.get("name") or "مشتری"),
            external_id=data.get("external_id"),
            phone=data.get("phone"),
            email=data.get("email"),
            telegram_user_id=int(data["telegram_user_id"]) if data.get("telegram_user_id") else None,
            tags=list(data.get("tags") or []),
            note=data.get("note"),
        )
        return row, {"portal_token": token, "portal_url": f"{settings.base_url}/customer/{row.id}/{token}"}
    elif resource == "payment-links":
        amount = data.get("fixed_amount_rial")
        if amount is None and data.get("fixed_amount_toman") not in (None, ""):
            amount = int(data["fixed_amount_toman"]) * 10
        row = await create_payment_link(
            session,
            merchant_id,
            title=str(data.get("title") or "لینک پرداخت"),
            store_id=store_id,
            product_id=int(data["product_id"]) if data.get("product_id") else None,
            campaign_id=int(data["campaign_id"]) if data.get("campaign_id") else None,
            amount_rial=int(amount) if amount not in (None, "") else None,
            min_amount_rial=int(data["min_amount_rial"]) if data.get("min_amount_rial") else None,
            max_amount_rial=int(data["max_amount_rial"]) if data.get("max_amount_rial") else None,
            description=data.get("description"),
            fee_mode=str(data.get("fee_mode") or "default"),
            ttl_minutes=int(data.get("ttl_minutes") or 30),
            completion_mode=str(data.get("completion_mode") or "exact"),
            collect_name=bool(data.get("collect_name", True)),
            collect_phone=bool(data.get("collect_phone", False)),
            collect_order_id=bool(data.get("collect_order_id", False)),
            max_uses=int(data["max_uses"]) if data.get("max_uses") else None,
            branding=data.get("branding") if isinstance(data.get("branding"), dict) else {},
        )
        return row, {"payment_url": f"{settings.base_url}/l/{row.slug}", "qr_payload": f"{settings.base_url}/l/{row.slug}"}
    elif resource == "automation-rules":
        actions = data.get("actions") or []
        if not isinstance(actions, list) or not actions:
            raise ValueError("حداقل یک Action برای اتوماسیون لازم است")
        row = AutomationRule(
            merchant_id=merchant_id,
            store_id=store_id,
            name=str(data.get("name") or "اتوماسیون")[:160],
            trigger=str(data.get("trigger") or "payment.paid")[:80],
            conditions_json=json_dumps(data.get("conditions") or {}),
            actions_json=json_dumps(actions),
            priority=int(data.get("priority") or 100),
        )
    elif resource == "connectors":
        base_url = data.get("base_url")
        if base_url:
            valid, normalized = validate_public_https_url(str(base_url))
            if not valid:
                raise ValueError(normalized)
            base_url = normalized
        row = IntegrationConnector(
            merchant_id=merchant_id,
            store_id=store_id,
            connector_type=str(data.get("connector_type") or "webhook")[:48],
            name=str(data.get("name") or "اتصال")[:140],
            base_url=base_url,
            config_encrypted=await encrypt_private_config(session, data.get("config") or {}),
            scopes_json=json_dumps(data.get("scopes") or []),
        )
    elif resource == "campaigns":
        row = Campaign(
            merchant_id=merchant_id,
            store_id=store_id,
            name=str(data.get("name") or "کمپین")[:140],
            code=str(data.get("code") or slugify(str(data.get("name") or "campaign"), "campaign"))[:80],
            source=data.get("source"),
            medium=data.get("medium"),
            budget_rial=int(data["budget_rial"]) if data.get("budget_rial") else None,
        )
    elif resource == "branches":
        row = Branch(
            merchant_id=merchant_id,
            store_id=store_id,
            name=str(data.get("name") or "شعبه")[:120],
            code=str(data.get("code") or slugify(str(data.get("name") or "branch"), "branch"))[:40],
            address=data.get("address"),
            manager_telegram_user_id=int(data["manager_telegram_user_id"]) if data.get("manager_telegram_user_id") else None,
        )
    elif resource == "message-templates":
        row = MessageTemplate(
            merchant_id=merchant_id,
            store_id=store_id,
            event_type=str(data.get("event_type") or "payment.paid")[:80],
            channel=str(data.get("channel") or "telegram")[:24],
            name=str(data.get("name") or "قالب پیام")[:120],
            body=str(data.get("body") or "پرداخت {token} تأیید شد."),
        )
    elif resource == "webhook-subscriptions":
        url = str(data.get("url") or "")
        valid, normalized = validate_public_https_url(url)
        if not valid:
            raise ValueError(normalized)
        row = WebhookSubscription(
            merchant_id=merchant_id,
            store_id=store_id,
            event_type=str(data.get("event_type") or "payment.paid")[:80],
            url=normalized,
            secret_encrypted=await encrypt_private_config(session, str(data.get("secret") or secrets.token_urlsafe(24))),
        )
    elif resource == "card-routing-rules":
        row = CardRoutingRule(
            merchant_id=merchant_id,
            store_id=store_id,
            name=str(data.get("name") or "قانون انتخاب کارت")[:140],
            priority=int(data.get("priority") or 100),
            conditions_json=json_dumps(data.get("conditions") or {}),
            card_id=int(data.get("card_id") or 0),
        )
    elif resource == "fraud-rules":
        row = FraudRule(
            merchant_id=merchant_id,
            store_id=store_id,
            name=str(data.get("name") or "قانون ضدتقلب")[:140],
            rule_code=str(data.get("rule_code") or slugify(str(data.get("name") or "rule"), "rule"))[:80],
            conditions_json=json_dumps(data.get("conditions") or {}),
            action=str(data.get("action") or "review")[:24],
            score=max(0, min(100, int(data.get("score") or 25))),
        )
    elif resource == "sms-parser-templates":
        # Regexes are stored disabled until explicitly tested/activated.
        row = SmsParserTemplate(
            merchant_id=merchant_id,
            bank_code=str(data.get("bank_code") or "generic")[:40],
            name=str(data.get("name") or "قالب پیامک")[:140],
            sender_pattern=data.get("sender_pattern"),
            credit_pattern=data.get("credit_pattern"),
            amount_pattern=str(data.get("amount_pattern") or ""),
            card_pattern=data.get("card_pattern"),
            reference_pattern=data.get("reference_pattern"),
            confidence=max(0, min(100, int(data.get("confidence") or 80))),
            is_active=False,
        )
    elif resource == "invoice-templates":
        amount = data.get("amount_rial")
        if amount is None and data.get("amount_toman") not in (None, ""):
            amount = int(data["amount_toman"]) * 10
        row = InvoiceTemplate(
            merchant_id=merchant_id, store_id=store_id,
            name=str(data.get("name") or "قالب فاکتور")[:140],
            amount_rial=int(amount) if amount not in (None, "") else None,
            description=data.get("description"), fee_mode=str(data.get("fee_mode") or "default")[:20],
            ttl_minutes=max(5, min(1440, int(data.get("ttl_minutes") or 30))),
            card_id=int(data["card_id"]) if data.get("card_id") else None,
            settings_json=json_dumps(data.get("settings") or {}),
        )
    elif resource == "subscription-plans":
        amount = int(data.get("amount_rial") or int(data.get("amount_toman") or 0) * 10)
        if amount < 10_000:
            raise ValueError("مبلغ پلن باید حداقل ۱٬۰۰۰ تومان باشد")
        row = SubscriptionPlan(
            merchant_id=merchant_id, store_id=store_id,
            product_id=int(data["product_id"]) if data.get("product_id") else None,
            name=str(data.get("name") or "پلن اشتراک")[:160], amount_rial=amount,
            interval_days=max(1, int(data.get("interval_days") or 30)),
            grace_days=max(0, int(data.get("grace_days") or 3)),
            max_cycles=int(data["max_cycles"]) if data.get("max_cycles") else None,
            auto_create_invoice=bool(data.get("auto_create_invoice", True)),
        )
    elif resource == "subscriptions":
        plan = await session.get(SubscriptionPlan, int(data.get("plan_id") or 0))
        customer = await session.get(Customer, int(data.get("customer_id") or 0))
        if not plan or plan.merchant_id != merchant_id or not customer or customer.merchant_id != merchant_id:
            raise ValueError("پلن یا مشتری معتبر نیست")
        started = _parse_datetime(data.get("started_at"), default=datetime.now(timezone.utc))
        row = Subscription(
            merchant_id=merchant_id, plan_id=plan.id, customer_id=customer.id, status="active",
            started_at=started, next_invoice_at=_parse_datetime(data.get("next_invoice_at"), default=started),
            expires_at=_parse_datetime(data.get("expires_at")),
        )
    elif resource == "discount-codes":
        code = str(data.get("code") or secrets.token_hex(4)).strip().upper()[:64]
        dtype = str(data.get("discount_type") or "percent")
        if dtype not in {"percent", "fixed"}:
            raise ValueError("نوع تخفیف باید percent یا fixed باشد")
        value = int(data.get("value") or 0)
        if value <= 0 or (dtype == "percent" and value > 100):
            raise ValueError("مقدار تخفیف معتبر نیست")
        row = DiscountCode(
            merchant_id=merchant_id, store_id=store_id, code=code, discount_type=dtype, value=value,
            min_amount_rial=int(data["min_amount_rial"]) if data.get("min_amount_rial") else None,
            max_discount_rial=int(data["max_discount_rial"]) if data.get("max_discount_rial") else None,
            max_uses=int(data["max_uses"]) if data.get("max_uses") else None,
            new_customers_only=bool(data.get("new_customers_only", False)),
            starts_at=_parse_datetime(data.get("starts_at")), ends_at=_parse_datetime(data.get("ends_at")),
        )
    elif resource == "affiliates":
        ctype = str(data.get("commission_type") or "percent")
        if ctype not in {"percent", "fixed"}:
            raise ValueError("نوع پورسانت باید percent یا fixed باشد")
        cvalue = int(data.get("commission_value") or 0)
        if cvalue < 0 or (ctype == "percent" and cvalue > 100):
            raise ValueError("پورسانت معتبر نیست")
        row = Affiliate(
            merchant_id=merchant_id, name=str(data.get("name") or "معرف")[:140],
            code=str(data.get("code") or secrets.token_hex(4)).strip().upper()[:64],
            telegram_user_id=int(data["telegram_user_id"]) if data.get("telegram_user_id") else None,
            commission_type=ctype, commission_value=cvalue,
        )
    elif resource == "support-tickets":
        row = SupportTicket(
            merchant_id=merchant_id, customer_id=int(data["customer_id"]) if data.get("customer_id") else None,
            invoice_id=int(data["invoice_id"]) if data.get("invoice_id") else None,
            subject=str(data.get("subject") or "درخواست پشتیبانی")[:180],
            category=str(data.get("category") or "general")[:64],
            priority=str(data.get("priority") or "normal")[:20],
            status="open", last_message_at=datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
        if data.get("message"):
            session.add(SupportMessage(ticket_id=row.id, sender_type="merchant", sender_id=str(merchant_id), body=str(data["message"]), attachment_url=data.get("attachment_url")))
        return row, {}
    elif resource == "payment-requests":
        amount = int(data.get("amount_rial") or int(data.get("amount_toman") or 0) * 10)
        if amount < 10_000:
            raise ValueError("مبلغ درخواست باید حداقل ۱٬۰۰۰ تومان باشد")
        row = PaymentRequest(
            merchant_id=merchant_id, customer_id=int(data["customer_id"]) if data.get("customer_id") else None,
            amount_rial=amount, description=data.get("description"), due_at=_parse_datetime(data.get("due_at")),
            delivery_channel=str(data.get("delivery_channel") or "telegram")[:24],
            delivery_target=data.get("delivery_target"), status="queued" if data.get("send_now", True) else "draft",
        )
    elif resource == "scheduled-invoices":
        template = await session.get(InvoiceTemplate, int(data.get("template_id") or 0))
        if not template or template.merchant_id != merchant_id:
            raise ValueError("قالب فاکتور معتبر نیست")
        row = ScheduledInvoice(
            merchant_id=merchant_id, customer_id=int(data["customer_id"]) if data.get("customer_id") else None,
            template_id=template.id, next_run_at=_parse_datetime(data.get("next_run_at"), default=datetime.now(timezone.utc)),
            interval_days=max(1, int(data.get("interval_days") or 30)),
            remaining_runs=int(data["remaining_runs"]) if data.get("remaining_runs") not in (None, "") else None,
        )
    elif resource in {"affiliate-commissions", "payment-reminders"}:
        raise ValueError("این منبع فقط خواندنی است")
    else:
        raise ValueError("منبع درخواستی پشتیبانی نمی‌شود")
    session.add(row)
    await session.flush()
    return row, {}


@router.get("/api/v1/options")
async def api_options_summary(context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    return {"success": True, **await option_summary(session, context.merchant.id)}


@router.get("/api/v1/options/integrations/catalog")
async def integration_catalog(context: ApiContext = Depends(api_context)):
    return {
        "success": True,
        "connectors": [
            {"type": "woocommerce", "name": "WooCommerce"},
            {"type": "wordpress", "name": "WordPress"},
            {"type": "whmcs", "name": "WHMCS"},
            {"type": "marzban", "name": "Marzban"},
            {"type": "pasarguard", "name": "Pasarguard"},
            {"type": "telegram", "name": "Telegram Bot"},
            {"type": "n8n", "name": "n8n"},
            {"type": "zapier", "name": "Zapier"},
            {"type": "make", "name": "Make"},
            {"type": "google_sheets", "name": "Google Sheets"},
            {"type": "custom_webhook", "name": "Custom Webhook"},
        ],
    }


@router.get("/api/v1/options/funnel")
async def api_options_funnel(days: int = 30, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    return {"success": True, "days": days, **await analytics_funnel(session, context.merchant.id, days)}


@router.get("/api/v1/options/{resource}")
async def api_list_options(resource: str, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    model = RESOURCE_MODELS.get(resource)
    if not model:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": "منبع یافت نشد"})
    rows = list((await session.scalars(_scope_stmt(model, context).order_by(model.id.desc()).limit(500))).all())
    return {"success": True, "items": [_serialize(row) for row in rows]}


@router.post("/api/v1/options/{resource}")
async def api_create_option(resource: str, request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    if resource in READ_ONLY_RESOURCES:
        raise HTTPException(status_code=405, detail={"code": "RESOURCE_READ_ONLY", "message": "این منبع فقط خواندنی است"})
    data = await _json_body(request)
    try:
        row, extra = await _create_resource(session, context, resource, data)
        await session.commit()
        return {"success": True, "item": _serialize(row), **extra}
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail={"code": "OPTION_CREATE_FAILED", "message": str(exc)})


@router.patch("/api/v1/options/{resource}/{item_id}")
async def api_update_option(resource: str, item_id: int, request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    model = RESOURCE_MODELS.get(resource)
    if not model:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": "منبع یافت نشد"})
    if resource in READ_ONLY_RESOURCES:
        raise HTTPException(status_code=405, detail={"code": "RESOURCE_READ_ONLY", "message": "این منبع فقط خواندنی است"})
    row = await session.get(model, item_id)
    if not row or getattr(row, "merchant_id", None) != context.merchant.id:
        raise HTTPException(status_code=404, detail={"code": "ITEM_NOT_FOUND", "message": "رکورد یافت نشد"})
    data = await _json_body(request)
    allowed = {c.name for c in row.__table__.columns} - {"id"} - SENSITIVE_FIELDS - SYSTEM_MANAGED_FIELDS
    for key, value in data.items():
        if key in allowed:
            setattr(row, key, value)
    await session.commit()
    return {"success": True, "item": _serialize(row)}


@router.delete("/api/v1/options/{resource}/{item_id}")
async def api_delete_option(resource: str, item_id: int, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    model = RESOURCE_MODELS.get(resource)
    if not model:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND", "message": "منبع یافت نشد"})
    if resource in READ_ONLY_RESOURCES:
        raise HTTPException(status_code=405, detail={"code": "RESOURCE_READ_ONLY", "message": "این منبع فقط خواندنی است"})
    row = await session.get(model, item_id)
    if not row or getattr(row, "merchant_id", None) != context.merchant.id:
        raise HTTPException(status_code=404, detail={"code": "ITEM_NOT_FOUND", "message": "رکورد یافت نشد"})
    if hasattr(row, "is_active"):
        row.is_active = False
    else:
        await session.delete(row)
    await session.commit()
    return {"success": True}


@router.post("/api/v1/options/customers/{customer_id}/portal-token")
async def rotate_customer_token(customer_id: int, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    customer = await session.get(Customer, customer_id)
    if not customer or customer.merchant_id != context.merchant.id:
        raise HTTPException(status_code=404, detail={"code": "CUSTOMER_NOT_FOUND", "message": "مشتری یافت نشد"})
    token = await rotate_customer_portal_token(session, customer)
    await session.commit()
    return {"success": True, "portal_url": f"{settings.base_url}/customer/{customer.id}/{token}", "portal_token": token}


@router.post("/api/v1/invoices/{token}/partial-payments")
async def api_partial_payment(token: str, request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    invoice = await session.scalar(select(Invoice).where(Invoice.token == token, Invoice.merchant_id == context.merchant.id))
    if not invoice:
        raise HTTPException(status_code=404, detail={"code": "INVOICE_NOT_FOUND", "message": "فاکتور یافت نشد"})
    data = await _json_body(request)
    amount = int(data.get("amount_rial") or int(data.get("amount_toman") or 0) * 10)
    try:
        payment, invoice = await record_partial_payment(
            session,
            invoice,
            amount_rial=amount,
            source="api",
            reference_number=data.get("reference_number"),
            note=data.get("note"),
        )
        await session.commit()
        return {
            "success": True,
            "partial_payment": _serialize(payment),
            "invoice_status": invoice.status,
            "received_amount_rial": invoice.received_amount_rial,
            "remaining_amount_rial": max(0, invoice.payable_amount_rial - invoice.received_amount_rial),
        }
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail={"code": "PARTIAL_PAYMENT_FAILED", "message": str(exc)})


@router.post("/api/v1/refunds")
async def api_refund(request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    data = await _json_body(request)
    invoice = await session.scalar(select(Invoice).where(Invoice.token == str(data.get("invoice_token") or ""), Invoice.merchant_id == context.merchant.id))
    if not invoice:
        raise HTTPException(status_code=404, detail={"code": "INVOICE_NOT_FOUND", "message": "فاکتور یافت نشد"})
    amount = int(data.get("amount_rial") or int(data.get("amount_toman") or 0) * 10)
    try:
        row = await create_refund_request(
            session,
            invoice,
            amount_rial=amount,
            reason=str(data.get("reason") or "درخواست بازپرداخت"),
            destination=data.get("destination"),
            requested_by=f"api:{context.api_key.id if context.api_key else 'legacy'}",
        )
        await session.commit()
        return {"success": True, "refund": _serialize(row)}
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail={"code": "REFUND_FAILED", "message": str(exc)})



@router.post("/api/v1/options/invoice-templates/{template_id}/create-invoice")
async def create_from_template_endpoint(template_id: int, request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    template = await session.get(InvoiceTemplate, template_id)
    if not template or template.merchant_id != context.merchant.id:
        raise HTTPException(status_code=404, detail={"code": "TEMPLATE_NOT_FOUND", "message": "قالب یافت نشد"})
    data = await _json_body(request)
    try:
        invoice = await create_invoice_from_template(session, template, customer_id=int(data["customer_id"]) if data.get("customer_id") else None)
        await session.commit()
        return {"success": True, "payment_id": invoice.token, "payment_url": f"{settings.base_url}/pay/{invoice.token}"}
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail={"code": "TEMPLATE_INVOICE_FAILED", "message": str(exc)})


@router.post("/api/v1/options/payment-requests/{request_id}/dispatch")
async def dispatch_payment_request_endpoint(request_id: int, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    row = await session.get(PaymentRequest, request_id)
    if not row or row.merchant_id != context.merchant.id:
        raise HTTPException(status_code=404, detail={"code": "REQUEST_NOT_FOUND", "message": "درخواست یافت نشد"})
    try:
        invoice = await dispatch_payment_request(session, row)
        await session.commit()
        return {"success": True, "payment_id": invoice.token, "payment_url": f"{settings.base_url}/pay/{invoice.token}"}
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail={"code": "REQUEST_DISPATCH_FAILED", "message": str(exc)})


@router.post("/api/v1/options/support-tickets/{ticket_id}/messages")
async def add_support_message(ticket_id: int, request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    ticket = await session.get(SupportTicket, ticket_id)
    if not ticket or ticket.merchant_id != context.merchant.id:
        raise HTTPException(status_code=404, detail={"code": "TICKET_NOT_FOUND", "message": "تیکت یافت نشد"})
    data = await _json_body(request)
    body = str(data.get("body") or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail={"code": "EMPTY_MESSAGE", "message": "متن پیام خالی است"})
    message = SupportMessage(ticket_id=ticket.id, sender_type="merchant", sender_id=str(context.merchant.id), body=body, attachment_url=data.get("attachment_url"))
    ticket.last_message_at = datetime.now(timezone.utc)
    session.add(message)
    await session.commit()
    return {"success": True, "message": _serialize(message)}


@router.get("/api/v1/options/support-tickets/{ticket_id}/messages")
async def list_support_messages(ticket_id: int, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    ticket = await session.get(SupportTicket, ticket_id)
    if not ticket or ticket.merchant_id != context.merchant.id:
        raise HTTPException(status_code=404, detail={"code": "TICKET_NOT_FOUND", "message": "تیکت یافت نشد"})
    rows = list((await session.scalars(select(SupportMessage).where(SupportMessage.ticket_id == ticket.id).order_by(SupportMessage.id.asc()))).all())
    return {"success": True, "items": [_serialize(row) for row in rows]}


@router.get("/l/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def public_payment_link(request: Request, slug: str, session: AsyncSession = Depends(get_session)):
    link = await session.scalar(select(PaymentLink).where(PaymentLink.slug == slug))
    if not link or not link.is_active:
        return templates.TemplateResponse("not_found.html", {"request": request}, status_code=404)
    product = await session.get(Product, link.product_id) if link.product_id else None
    merchant = await session.get(Merchant, link.merchant_id)
    verification = None
    if merchant:
        from app.models import MerchantVerification
        verification = await session.scalar(select(MerchantVerification).where(MerchantVerification.merchant_id == merchant.id))
    session_id = request.cookies.get("bp_session") or secrets.token_urlsafe(12)
    experiment_id, variant_id, variant_config = await select_ab_variant(session, link.merchant_id, link.store_id, session_id)
    await record_analytics_event(
        session,
        merchant_id=link.merchant_id,
        store_id=link.store_id,
        payment_link_id=link.id,
        campaign_id=link.campaign_id,
        experiment_id=experiment_id,
        variant_id=variant_id,
        session_id=session_id,
        source=request.query_params.get("source"),
        event_type="payment_link.viewed",
        metadata={"referer": request.headers.get("referer")},
    )
    await session.commit()
    response = templates.TemplateResponse("payment_link.html", {
        "request": request,
        "link": link,
        "product": product,
        "merchant": merchant,
        "verification": verification,
        "branding": json_loads(link.branding_json, {}),
        "variant": variant_config,
        "app_version": APP_VERSION,
    })
    response.set_cookie("bp_session", session_id, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax", secure=settings.base_url.startswith("https://"))
    return response


@router.post("/l/{slug}", include_in_schema=False)
async def submit_payment_link(
    request: Request,
    slug: str,
    amount_toman: str = Form(default=""),
    customer_name: str = Form(default=""),
    phone: str = Form(default=""),
    order_id: str = Form(default=""),
    discount_code: str = Form(default=""),
    affiliate_code: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
):
    link = await session.scalar(select(PaymentLink).where(PaymentLink.slug == slug))
    if not link:
        return templates.TemplateResponse("not_found.html", {"request": request}, status_code=404)
    try:
        amount_rial = int(str(amount_toman).replace(",", "").strip()) * 10 if amount_toman.strip() else None
        invoice = await create_invoice_from_payment_link(
            session,
            link,
            amount_rial=amount_rial,
            customer_name=customer_name.strip() or None,
            phone=phone.strip() or None,
            order_id=order_id.strip() or None,
            source=request.query_params.get("source") or "web",
            session_id=request.cookies.get("bp_session") or secrets.token_urlsafe(12),
            discount_code=discount_code.strip() or None,
            affiliate_code=affiliate_code.strip() or request.query_params.get("affiliate"),
        )
        await session.commit()
        return RedirectResponse(url=f"{settings.base_url}/pay/{invoice.token}", status_code=303)
    except ValueError as exc:
        await session.rollback()
        product = await session.get(Product, link.product_id) if link.product_id else None
        merchant = await session.get(Merchant, link.merchant_id)
        return templates.TemplateResponse("payment_link.html", {
            "request": request,
            "link": link,
            "product": product,
            "merchant": merchant,
            "verification": None,
            "branding": json_loads(link.branding_json, {}),
            "variant": {},
            "error": str(exc),
            "app_version": APP_VERSION,
        }, status_code=400)


@router.get("/customer/{customer_id}/{token}", response_class=HTMLResponse, include_in_schema=False)
async def customer_portal(request: Request, customer_id: int, token: str, session: AsyncSession = Depends(get_session)):
    customer = await get_customer_by_portal_token(session, customer_id, token)
    if not customer:
        return templates.TemplateResponse("not_found.html", {"request": request}, status_code=404)
    dashboard = await customer_portal_dashboard(session, customer)
    merchant = await session.get(Merchant, customer.merchant_id)
    return templates.TemplateResponse("customer_portal.html", {
        "request": request,
        "merchant": merchant,
        "token": token,
        **dashboard,
        "app_version": APP_VERSION,
    }, headers={"Cache-Control": "no-store"})


@router.get("/portal/{merchant_id}/{token}/options", response_class=HTMLResponse, include_in_schema=False)
async def merchant_options_portal(request: Request, merchant_id: int, token: str, session: AsyncSession = Depends(get_session)):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not verify_portal_token(merchant, token):
        return templates.TemplateResponse("not_found.html", {"request": request}, status_code=404)
    summary = await option_summary(session, merchant.id)
    resources: dict[str, list[Any]] = {}
    portal_context = ApiContext(merchant=merchant, store=None, legacy=True)
    for key, model in RESOURCE_MODELS.items():
        if key == "refunds":
            continue
        # Some option resources (notably AbVariant) are scoped through a
        # parent table and do not expose merchant_id directly. Reuse the same
        # scope builder as the public API instead of assuming every model has
        # a merchant_id column.
        stmt = _scope_stmt(model, portal_context).order_by(model.id.desc()).limit(50)
        resources[key] = list((await session.scalars(stmt)).all())
    funnel = await analytics_funnel(session, merchant.id, 30)
    return templates.TemplateResponse("options.html", {
        "request": request,
        "merchant": merchant,
        "token": token,
        "summary": summary,
        "resources": resources,
        "funnel": funnel,
        "base_url": settings.base_url,
        "app_version": APP_VERSION,
    }, headers={"Cache-Control": "no-store"})


@router.post("/portal/{merchant_id}/{token}/options/{resource}", include_in_schema=False)
async def portal_create_option(request: Request, merchant_id: int, token: str, resource: str, session: AsyncSession = Depends(get_session)):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not verify_portal_token(merchant, token):
        raise HTTPException(status_code=404, detail="not found")
    form = await request.form()
    data: dict[str, Any] = dict(form)
    for key in ("conditions", "actions", "config", "scopes", "tags", "branding", "settings"):
        if key in data:
            try:
                data[key] = json.loads(str(data[key]))
            except Exception:
                data[key] = [] if key in {"actions", "scopes", "tags"} else {}
    context = ApiContext(merchant=merchant, store=None, legacy=True)
    try:
        await _create_resource(session, context, resource, data)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        return RedirectResponse(url=f"/portal/{merchant_id}/{token}/options?error={str(exc)[:120]}", status_code=303)
    return RedirectResponse(url=f"/portal/{merchant_id}/{token}/options?created={resource}", status_code=303)


@router.get("/api/v1/commerce/profile")
async def commerce_profile(context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    row = await ensure_option_profile(session, context.merchant.id)
    await session.commit()
    return {"success": True, "profile": _serialize(row), "notifications": json_loads(row.notifications_json, {}), "feature_flags": json_loads(row.feature_flags_json, {})}


@router.patch("/api/v1/commerce/profile")
async def commerce_profile_update(request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    data = await _json_body(request)
    row = await ensure_option_profile(session, context.merchant.id)
    if "locale" in data:
        row.locale = str(data["locale"])[:12]
    if "timezone" in data:
        row.timezone = str(data["timezone"])[:64]
    if "retention_days" in data:
        row.retention_days = max(30, min(3650, int(data["retention_days"])))
    if "low_balance_threshold_rial" in data:
        row.low_balance_threshold_rial = max(0, int(data["low_balance_threshold_rial"]))
    if "notifications" in data and isinstance(data["notifications"], dict):
        row.notifications_json = json_dumps(data["notifications"])
    if "feature_flags" in data and isinstance(data["feature_flags"], dict):
        row.feature_flags_json = json_dumps(data["feature_flags"])
    if "emergency_mode" in data:
        row.emergency_mode = bool(data["emergency_mode"])
    if "public_status_enabled" in data:
        row.public_status_enabled = bool(data["public_status_enabled"])
    if "custom_domain" in data:
        domain = str(data["custom_domain"] or "").strip().lower()
        row.custom_domain = domain or None
    if data.get("rotate_anti_phishing_code"):
        row.anti_phishing_code = secrets.token_hex(3).upper()
    await session.commit()
    return {"success": True, "profile": _serialize(row), "notifications": json_loads(row.notifications_json, {}), "feature_flags": json_loads(row.feature_flags_json, {})}


@router.post("/api/v1/commerce/verification/request")
async def request_merchant_verification(request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    data = await _json_body(request)
    row = await session.scalar(select(MerchantVerification).where(MerchantVerification.merchant_id == context.merchant.id))
    if not row:
        row = MerchantVerification(merchant_id=context.merchant.id)
        session.add(row)
    row.level = str(data.get("level") or "business")[:32]
    row.status = "pending"
    row.business_name = str(data.get("business_name") or context.merchant.name)[:180]
    row.verified_domain = str(data.get("domain") or "")[:255] or None
    row.evidence_json = json_dumps(data.get("evidence") or {})
    row.trust_badge_enabled = False
    await session.commit()
    return {"success": True, "verification": _serialize(row)}


@router.post("/api/v1/commerce/connectors/{connector_id}/test")
async def test_connector(connector_id: int, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    row = await session.get(IntegrationConnector, connector_id)
    if not row or row.merchant_id != context.merchant.id:
        raise HTTPException(status_code=404, detail={"code": "CONNECTOR_NOT_FOUND", "message": "اتصال یافت نشد"})
    if not row.base_url:
        raise HTTPException(status_code=400, detail={"code": "CONNECTOR_URL_MISSING", "message": "نشانی اتصال ثبت نشده است"})
    valid, normalized = validate_public_https_url(row.base_url)
    if not valid:
        raise HTTPException(status_code=400, detail={"code": "CONNECTOR_URL_INVALID", "message": normalized})
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(normalized, headers={"User-Agent": "BluePay-Connector-Test/1.2"})
        row.last_test_status = "ok" if response.status_code < 500 else "failed"
        row.last_error = None if response.status_code < 500 else f"HTTP {response.status_code}"
        row.last_test_at = datetime.now(timezone.utc)
        await session.commit()
        return {"success": response.status_code < 500, "http_status": response.status_code, "preview": response.text[:300]}
    except Exception as exc:
        row.last_test_status = "failed"
        row.last_error = f"{type(exc).__name__}: {exc}"[:1000]
        row.last_test_at = datetime.now(timezone.utc)
        await session.commit()
        raise HTTPException(status_code=502, detail={"code": "CONNECTOR_TEST_FAILED", "message": row.last_error})


@router.post("/api/v1/commerce/parser/{template_id}/test")
async def test_parser_template(template_id: int, request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    row = await session.get(SmsParserTemplate, template_id)
    if not row or row.merchant_id not in {None, context.merchant.id}:
        raise HTTPException(status_code=404, detail={"code": "PARSER_NOT_FOUND", "message": "قالب یافت نشد"})
    data = await _json_body(request)
    sample = str(data.get("message") or "")[:3000]
    if not sample:
        raise HTTPException(status_code=400, detail={"code": "SAMPLE_REQUIRED", "message": "متن نمونه پیامک لازم است"})
    try:
        amount_match = re.search(row.amount_pattern[:500], sample, flags=re.IGNORECASE)
        card_match = re.search(row.card_pattern[:500], sample, flags=re.IGNORECASE) if row.card_pattern else None
        reference_match = re.search(row.reference_pattern[:500], sample, flags=re.IGNORECASE) if row.reference_pattern else None
        credit_ok = bool(re.search(row.credit_pattern[:500], sample, flags=re.IGNORECASE)) if row.credit_pattern else True
    except re.error as exc:
        raise HTTPException(status_code=400, detail={"code": "REGEX_INVALID", "message": str(exc)})
    row.tested_count += 1
    if amount_match and credit_ok and data.get("activate"):
        row.is_active = True
    await session.commit()
    return {
        "success": bool(amount_match and credit_ok),
        "amount": amount_match.group(1) if amount_match and amount_match.groups() else (amount_match.group(0) if amount_match else None),
        "card": card_match.group(1) if card_match and card_match.groups() else (card_match.group(0) if card_match else None),
        "reference": reference_match.group(1) if reference_match and reference_match.groups() else (reference_match.group(0) if reference_match else None),
        "credit_intent": credit_ok,
        "activated": row.is_active,
    }


@router.post("/api/v1/commerce/inbox/{item_id}/resolve")
async def resolve_inbox_item(item_id: int, request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    row = await session.get(AdminInboxItem, item_id)
    if not row or row.merchant_id not in {None, context.merchant.id}:
        raise HTTPException(status_code=404, detail={"code": "INBOX_NOT_FOUND", "message": "مورد یافت نشد"})
    data = await _json_body(request)
    row.status = str(data.get("status") or "resolved")[:24]
    row.assigned_to = str(data.get("assigned_to") or context.merchant.telegram_user_id)[:120]
    row.resolved_at = datetime.now(timezone.utc) if row.status in {"resolved", "closed", "rejected"} else None
    if data.get("note"):
        row.detail = (row.detail or "") + "\n\nنتیجه: " + str(data["note"])
    await session.commit()
    return {"success": True, "item": _serialize(row)}


@router.post("/api/v1/commerce/customers/{customer_id}/wallet")
async def adjust_customer_wallet(customer_id: int, request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    customer = await session.get(Customer, customer_id)
    if not customer or customer.merchant_id != context.merchant.id:
        raise HTTPException(status_code=404, detail={"code": "CUSTOMER_NOT_FOUND", "message": "مشتری یافت نشد"})
    data = await _json_body(request)
    wallet = await session.scalar(select(CustomerWallet).where(CustomerWallet.merchant_id == context.merchant.id, CustomerWallet.customer_id == customer.id))
    if not wallet:
        wallet = CustomerWallet(merchant_id=context.merchant.id, customer_id=customer.id)
        session.add(wallet)
        await session.flush()
    amount = int(data.get("amount_rial") or 0)
    points = int(data.get("points") or 0)
    key = str(data.get("idempotency_key") or secrets.token_urlsafe(16))[:180]
    exists = await session.scalar(select(CustomerWalletEntry).where(CustomerWalletEntry.idempotency_key == key))
    if exists:
        return {"success": True, "duplicate": True, "wallet": _serialize(wallet)}
    wallet.balance_rial += amount
    wallet.points += points
    if wallet.points >= 10_000:
        wallet.tier = "gold"
    elif wallet.points >= 3_000:
        wallet.tier = "silver"
    else:
        wallet.tier = "bronze"
    session.add(CustomerWalletEntry(
        wallet_id=wallet.id,
        entry_type=str(data.get("entry_type") or "manual_adjustment")[:40],
        amount_rial=amount,
        points=points,
        reference_type=data.get("reference_type"),
        reference_id=data.get("reference_id"),
        description=data.get("description"),
        idempotency_key=key,
    ))
    await session.commit()
    return {"success": True, "wallet": _serialize(wallet)}


@router.post("/api/v1/commerce/shifts/open")
async def open_cashier_shift(request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    data = await _json_body(request)
    branch = await session.get(Branch, int(data.get("branch_id") or 0))
    if not branch or branch.merchant_id != context.merchant.id or not branch.is_active:
        raise HTTPException(status_code=404, detail={"code": "BRANCH_NOT_FOUND", "message": "شعبه یافت نشد"})
    cashier = int(data.get("cashier_telegram_user_id") or context.merchant.telegram_user_id)
    existing = await session.scalar(select(CashierShift).where(CashierShift.branch_id == branch.id, CashierShift.cashier_telegram_user_id == cashier, CashierShift.status == "open"))
    if existing:
        return {"success": True, "shift": _serialize(existing), "duplicate": True}
    row = CashierShift(
        merchant_id=context.merchant.id,
        branch_id=branch.id,
        cashier_telegram_user_id=cashier,
        opened_at=datetime.now(timezone.utc),
        opening_amount_rial=int(data.get("opening_amount_rial") or 0),
    )
    session.add(row)
    await session.commit()
    return {"success": True, "shift": _serialize(row)}


@router.post("/api/v1/commerce/shifts/{shift_id}/close")
async def close_cashier_shift(shift_id: int, request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    row = await session.get(CashierShift, shift_id)
    if not row or row.merchant_id != context.merchant.id:
        raise HTTPException(status_code=404, detail={"code": "SHIFT_NOT_FOUND", "message": "شیفت یافت نشد"})
    data = await _json_body(request)
    row.status = "closed"
    row.closed_at = datetime.now(timezone.utc)
    row.closing_amount_rial = int(data.get("closing_amount_rial") or 0)
    await session.commit()
    return {"success": True, "shift": _serialize(row)}


@router.get("/api/v1/commerce/jobs")
async def commerce_jobs(context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    rows = list((await session.scalars(select(FulfillmentJob).where(FulfillmentJob.merchant_id == context.merchant.id).order_by(FulfillmentJob.id.desc()).limit(200))).all())
    return {"success": True, "items": [_serialize(row) for row in rows]}


@router.post("/api/v1/commerce/ab-experiments")
async def create_ab_experiment(request: Request, context: ApiContext = Depends(api_context), session: AsyncSession = Depends(get_session)):
    data = await _json_body(request)
    variants = data.get("variants") or []
    if not isinstance(variants, list) or len(variants) < 2:
        raise HTTPException(status_code=400, detail={"code": "VARIANTS_REQUIRED", "message": "حداقل دو Variant لازم است"})
    row = AbExperiment(
        merchant_id=context.merchant.id,
        store_id=context.store.id if context.store else data.get("store_id"),
        name=str(data.get("name") or "A/B Test")[:140],
        target=str(data.get("target") or "payment_page")[:48],
        status=str(data.get("status") or "draft")[:24],
        allocation_percent=max(1, min(100, int(data.get("allocation_percent") or 100))),
    )
    session.add(row)
    await session.flush()
    created = []
    for item in variants[:10]:
        variant = AbVariant(
            experiment_id=row.id,
            name=str(item.get("name") or f"Variant {len(created)+1}")[:100],
            weight=max(1, int(item.get("weight") or 50)),
            config_json=json_dumps(item.get("config") or {}),
        )
        session.add(variant)
        created.append(variant)
    await session.commit()
    return {"success": True, "experiment": _serialize(row), "variants": [_serialize(v) for v in created]}


@router.get("/qr/payment-link/{slug}.png", include_in_schema=False)
async def payment_link_qr(slug: str, session: AsyncSession = Depends(get_session)):
    link = await session.scalar(select(PaymentLink).where(PaymentLink.slug == slug, PaymentLink.is_active.is_(True)))
    if not link:
        raise HTTPException(status_code=404, detail="not found")
    try:
        import qrcode
        qr = qrcode.QRCode(version=None, box_size=8, border=3)
        qr.add_data(f"{settings.base_url}/l/{link.slug}?source=qr")
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="QR dependency is not installed") from exc


@router.get("/portal/{merchant_id}/{token}/pos", response_class=HTMLResponse, include_in_schema=False)
async def pos_terminal(request: Request, merchant_id: int, token: str, session: AsyncSession = Depends(get_session)):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not verify_portal_token(merchant, token):
        return templates.TemplateResponse("not_found.html", {"request": request}, status_code=404)
    branches = list((await session.scalars(select(Branch).where(Branch.merchant_id == merchant.id, Branch.is_active.is_(True)).order_by(Branch.id))).all())
    return templates.TemplateResponse("pos.html", {"request": request, "merchant": merchant, "token": token, "branches": branches, "app_version": APP_VERSION}, headers={"Cache-Control": "no-store"})


@router.post("/portal/{merchant_id}/{token}/pos", response_class=HTMLResponse, include_in_schema=False)
async def pos_create_invoice(
    request: Request,
    merchant_id: int,
    token: str,
    amount_toman: str = Form(...),
    description: str = Form(default="فروش حضوری"),
    branch_id: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
):
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not verify_portal_token(merchant, token):
        return templates.TemplateResponse("not_found.html", {"request": request}, status_code=404)
    try:
        amount_rial = int(amount_toman.replace(",", "").strip()) * 10
        branch = await session.get(Branch, int(branch_id)) if branch_id else None
        if branch and branch.merchant_id != merchant.id:
            branch = None
        invoice = await create_invoice(
            session,
            merchant,
            base_amount_rial=amount_rial,
            description=description,
            store_id=branch.store_id if branch else None,
            source_channel="pos",
        )
        invoice.branch_id = branch.id if branch else None
        await session.commit()
        payment_url = f"{settings.base_url}/pay/{invoice.token}"
        branches = list((await session.scalars(select(Branch).where(Branch.merchant_id == merchant.id, Branch.is_active.is_(True)).order_by(Branch.id))).all())
        return templates.TemplateResponse("pos.html", {"request": request, "merchant": merchant, "token": token, "branches": branches, "invoice": invoice, "payment_url": payment_url, "app_version": APP_VERSION}, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        await session.rollback()
        branches = list((await session.scalars(select(Branch).where(Branch.merchant_id == merchant.id, Branch.is_active.is_(True)).order_by(Branch.id))).all())
        return templates.TemplateResponse("pos.html", {"request": request, "merchant": merchant, "token": token, "branches": branches, "error": str(exc), "app_version": APP_VERSION}, status_code=400)


@router.post("/webhooks/sms/{merchant_id}/{token}/batch")
async def sms_offline_batch(merchant_id: int, token: str, request: Request, session: AsyncSession = Depends(get_session)):
    """Idempotent batch endpoint for SMS Forwarder offline queues."""
    merchant = await session.get(Merchant, merchant_id)
    if not merchant or not merchant.is_active or not merchant.callback_secret:
        raise HTTPException(status_code=404, detail={"code": "SMS_WEBHOOK_NOT_FOUND", "message": "Webhook یافت نشد"})
    if not hmac.compare_digest(token, merchant_sms_token(merchant)):
        raise HTTPException(status_code=401, detail={"code": "INVALID_SMS_WEBHOOK_TOKEN", "message": "Webhook token نامعتبر است"})
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_JSON", "message": "بدنه JSON معتبر نیست"}) from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    device_id = str(payload.get("device_id") or "") if isinstance(payload, dict) else ""
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise HTTPException(status_code=400, detail={"code": "INVALID_BATCH", "message": "items باید بین ۱ تا ۱۰۰ پیامک داشته باشد"})
    registered_count = int(await session.scalar(select(func.count(SmsDevice.id)).where(SmsDevice.merchant_id == merchant.id, SmsDevice.is_active.is_(True))) or 0)
    if registered_count:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        try:
            device = await authenticate_sms_device(
                session,
                merchant,
                device_id=device_id,
                raw_body=raw,
                timestamp=request.headers.get("X-BluePay-Timestamp", ""),
                signature=request.headers.get("X-BluePay-Signature", ""),
                bank_code=None,
                ip_address=forwarded or (request.client.host if request.client else None),
            )
        except PermissionError as exc:
            await session.rollback()
            raise HTTPException(status_code=403, detail={"code": "SMS_DEVICE_SIGNATURE_INVALID", "message": str(exc)})
        if not device:
            raise HTTPException(status_code=403, detail={"code": "SMS_DEVICE_NOT_REGISTERED", "message": "دستگاه ثبت نشده است"})
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sms, invoice, diagnostic = await ingest_sms(
            session,
            str(item.get("sender") or "")[:120],
            str(item.get("message") or "")[:8000],
            str(item.get("device_id") or device_id or "")[:120] or None,
            merchant_id=merchant.id,
            bank_hint=str(item.get("bank_code") or "")[:40] or None,
        )
        results.append({"client_id": item.get("client_id"), "sms_id": sms.id, "result": diagnostic.result, "invoice_token": invoice.token if invoice else None})
    await session.commit()
    return {"success": True, "processed": len(results), "results": results}


@router.get("/qr/invoice/{token}.png", include_in_schema=False)
async def invoice_qr(token: str, session: AsyncSession = Depends(get_session)):
    invoice = await session.scalar(select(Invoice).where(Invoice.token == token))
    if not invoice:
        raise HTTPException(status_code=404, detail="not found")
    try:
        import qrcode
        qr = qrcode.QRCode(version=None, box_size=8, border=3)
        qr.add_data(f"{settings.base_url}/pay/{invoice.token}")
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="QR dependency is not installed") from exc
