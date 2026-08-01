from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import decrypt_text, encrypt_text, sha256_text
from app.core.urls import validate_public_https_url
from app.models import (
    AbExperiment,
    AbVariant,
    AdminInboxItem,
    AnalyticsEvent,
    AutomationExecution,
    AutomationRule,
    BankCard,
    Branch,
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
    PartialPayment,
    PaymentLink,
    Product,
    RefundRequest,
    Store,
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
from app.services.invoice_service import confirm_invoice_paid, create_invoice
from app.services.settings_service import get_setting
from app.services.timeline_service import record_payment_event


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def slugify(value: str, fallback: str = "item") -> str:
    value = (value or "").strip().casefold()
    value = re.sub(r"[^a-z0-9\u0600-\u06ff_-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-_")
    return (value[:80] or fallback) + "-" + secrets.token_hex(3)


async def ensure_option_profile(session: AsyncSession, merchant_id: int) -> MerchantOptionProfile:
    row = await session.scalar(select(MerchantOptionProfile).where(MerchantOptionProfile.merchant_id == merchant_id))
    if row:
        return row
    row = MerchantOptionProfile(
        merchant_id=merchant_id,
        notifications_json=json_dumps({
            "payment_paid": True,
            "payment_failed": True,
            "callback_failed": True,
            "low_balance": True,
            "sms_device_offline": True,
            "daily_report": False,
        }),
        feature_flags_json=json_dumps({
            "automation": True,
            "partial_payments": True,
            "customer_portal": True,
            "loyalty": True,
            "pos": True,
            "ab_testing": True,
            "working_hours": {"enabled": False, "days": {}},
            "abandoned_payment_reminder_minutes": 15,
        }),
        anti_phishing_code=secrets.token_hex(3).upper(),
    )
    session.add(row)
    await session.flush()
    return row


async def option_summary(session: AsyncSession, merchant_id: int) -> dict[str, Any]:
    profile = await ensure_option_profile(session, merchant_id)
    models = {
        "products": Product,
        "customers": Customer,
        "payment_links": PaymentLink,
        "branches": Branch,
        "campaigns": Campaign,
        "automation_rules": AutomationRule,
        "connectors": IntegrationConnector,
        "refunds_open": RefundRequest,
        "jobs_pending": FulfillmentJob,
        "inbox_open": AdminInboxItem,
        "invoice_templates": InvoiceTemplate,
        "subscription_plans": SubscriptionPlan,
        "subscriptions": Subscription,
        "discount_codes": DiscountCode,
        "affiliates": Affiliate,
        "support_tickets": SupportTicket,
        "payment_requests": PaymentRequest,
        "scheduled_invoices": ScheduledInvoice,
    }
    result: dict[str, int] = {}
    for key, model in models.items():
        stmt = select(func.count(model.id)).where(model.merchant_id == merchant_id)
        if key in {"refunds_open", "jobs_pending", "inbox_open"}:
            status_col = getattr(model, "status")
            stmt = stmt.where(status_col.in_(["requested", "pending", "retry", "open", "review"]))
        result[key] = int(await session.scalar(stmt) or 0)
    verification = await session.scalar(select(MerchantVerification).where(MerchantVerification.merchant_id == merchant_id))
    result["profile"] = {
        "locale": profile.locale,
        "timezone": profile.timezone,
        "retention_days": profile.retention_days,
        "emergency_mode": profile.emergency_mode,
        "custom_domain": profile.custom_domain,
        "anti_phishing_code": profile.anti_phishing_code,
        "notifications": json_loads(profile.notifications_json, {}),
        "feature_flags": json_loads(profile.feature_flags_json, {}),
    }
    result["verification"] = {
        "level": verification.level if verification else "phone",
        "status": verification.status if verification else "not_requested",
        "trust_badge_enabled": bool(verification and verification.trust_badge_enabled),
    }
    return result


async def encrypt_private_config(session: AsyncSession, value: dict[str, Any] | str | None) -> str | None:
    if value in (None, "", {}):
        return None
    key = await get_setting(session, "encryption_key") or ""
    raw = value if isinstance(value, str) else json_dumps(value)
    return encrypt_text(raw, key)


async def decrypt_private_config(session: AsyncSession, value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    key = await get_setting(session, "encryption_key") or ""
    try:
        return json_loads(decrypt_text(value, key), {})
    except Exception:
        return {}


async def create_customer(
    session: AsyncSession,
    merchant_id: int,
    *,
    name: str,
    external_id: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    telegram_user_id: int | None = None,
    tags: list[str] | None = None,
    note: str | None = None,
) -> tuple[Customer, str]:
    key = await get_setting(session, "encryption_key") or ""
    token = secrets.token_urlsafe(32)
    row = Customer(
        merchant_id=merchant_id,
        external_id=(external_id or None),
        name=(name or "مشتری")[:160],
        phone_encrypted=encrypt_text(phone.strip(), key) if phone else None,
        phone_last4=re.sub(r"\D", "", phone or "")[-4:] or None,
        email_encrypted=encrypt_text(email.strip(), key) if email else None,
        telegram_user_id=telegram_user_id,
        tags_json=json_dumps(tags or []),
        note=note,
        portal_token_hash=sha256_text(token),
    )
    session.add(row)
    await session.flush()
    wallet = CustomerWallet(merchant_id=merchant_id, customer_id=row.id)
    session.add(wallet)
    await session.flush()
    return row, token


async def rotate_customer_portal_token(session: AsyncSession, customer: Customer) -> str:
    token = secrets.token_urlsafe(32)
    customer.portal_token_version += 1
    customer.portal_token_hash = sha256_text(token)
    await session.flush()
    return token


async def get_customer_by_portal_token(session: AsyncSession, customer_id: int, token: str) -> Customer | None:
    row = await session.get(Customer, customer_id)
    if not row or not row.portal_token_hash:
        return None
    return row if hmac.compare_digest(row.portal_token_hash, sha256_text(token)) else None


async def create_payment_link(
    session: AsyncSession,
    merchant_id: int,
    *,
    title: str,
    store_id: int | None = None,
    product_id: int | None = None,
    campaign_id: int | None = None,
    amount_rial: int | None = None,
    min_amount_rial: int | None = None,
    max_amount_rial: int | None = None,
    description: str | None = None,
    fee_mode: str = "default",
    ttl_minutes: int = 30,
    completion_mode: str = "exact",
    collect_name: bool = True,
    collect_phone: bool = False,
    collect_order_id: bool = False,
    max_uses: int | None = None,
    expires_at: datetime | None = None,
    branding: dict[str, Any] | None = None,
) -> PaymentLink:
    if amount_rial is not None and amount_rial < 10_000:
        raise ValueError("مبلغ ثابت باید حداقل ۱٬۰۰۰ تومان باشد")
    if completion_mode not in {"exact", "partial"}:
        raise ValueError("حالت تکمیل پرداخت نامعتبر است")
    row = PaymentLink(
        merchant_id=merchant_id,
        store_id=store_id,
        product_id=product_id,
        campaign_id=campaign_id,
        slug=slugify(title, "pay"),
        title=title[:180],
        description=description,
        fixed_amount_rial=amount_rial,
        min_amount_rial=min_amount_rial,
        max_amount_rial=max_amount_rial,
        fee_mode=fee_mode,
        ttl_minutes=max(5, min(1440, ttl_minutes)),
        completion_mode=completion_mode,
        collect_name=collect_name,
        collect_phone=collect_phone,
        collect_order_id=collect_order_id,
        max_uses=max_uses,
        expires_at=expires_at,
        branding_json=json_dumps(branding or {}),
    )
    session.add(row)
    await session.flush()
    return row


async def record_analytics_event(
    session: AsyncSession,
    *,
    merchant_id: int,
    event_type: str,
    store_id: int | None = None,
    invoice_id: int | None = None,
    payment_link_id: int | None = None,
    campaign_id: int | None = None,
    experiment_id: int | None = None,
    variant_id: int | None = None,
    session_id: str | None = None,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AnalyticsEvent:
    row = AnalyticsEvent(
        merchant_id=merchant_id,
        store_id=store_id,
        invoice_id=invoice_id,
        payment_link_id=payment_link_id,
        campaign_id=campaign_id,
        experiment_id=experiment_id,
        variant_id=variant_id,
        session_id=session_id,
        event_type=event_type,
        source=source,
        metadata_json=json_dumps(metadata or {}),
        occurred_at=utcnow(),
    )
    session.add(row)
    return row


async def select_ab_variant(session: AsyncSession, merchant_id: int, store_id: int | None, session_id: str) -> tuple[int | None, int | None, dict[str, Any]]:
    now = utcnow()
    experiment = await session.scalar(
        select(AbExperiment)
        .where(
            AbExperiment.merchant_id == merchant_id,
            AbExperiment.target == "payment_page",
            AbExperiment.status == "running",
            or_(AbExperiment.store_id.is_(None), AbExperiment.store_id == store_id),
            or_(AbExperiment.starts_at.is_(None), AbExperiment.starts_at <= now),
            or_(AbExperiment.ends_at.is_(None), AbExperiment.ends_at >= now),
        )
        .order_by(AbExperiment.store_id.desc(), AbExperiment.id.desc())
        .limit(1)
    )
    if not experiment:
        return None, None, {}
    bucket = int(hashlib.sha256(f"{experiment.id}:{session_id}".encode()).hexdigest()[:8], 16) % 100
    if bucket >= max(0, min(100, experiment.allocation_percent)):
        return experiment.id, None, {}
    variants = list((await session.scalars(select(AbVariant).where(AbVariant.experiment_id == experiment.id).order_by(AbVariant.id))).all())
    if not variants:
        return experiment.id, None, {}
    total = sum(max(1, item.weight) for item in variants)
    point = int(hashlib.sha256(f"variant:{session_id}".encode()).hexdigest()[:8], 16) % total
    cursor = 0
    selected = variants[0]
    for item in variants:
        cursor += max(1, item.weight)
        if point < cursor:
            selected = item
            break
    selected.views += 1
    return experiment.id, selected.id, json_loads(selected.config_json, {})




def _utc_value(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def resolve_discount_code(
    session: AsyncSession,
    *,
    merchant_id: int,
    store_id: int | None,
    code: str | None,
    amount_rial: int,
    customer: Customer | None = None,
) -> tuple[DiscountCode | None, int]:
    normalized = (code or "").strip().upper()
    if not normalized:
        return None, 0
    now = utcnow()
    row = await session.scalar(
        select(DiscountCode).where(
            DiscountCode.merchant_id == merchant_id,
            func.upper(DiscountCode.code) == normalized,
            DiscountCode.is_active.is_(True),
            or_(DiscountCode.store_id.is_(None), DiscountCode.store_id == store_id),
        ).order_by(DiscountCode.store_id.desc(), DiscountCode.id.desc()).limit(1)
    )
    if not row:
        raise ValueError("کد تخفیف معتبر نیست")
    if _utc_value(row.starts_at) and _utc_value(row.starts_at) > now:
        raise ValueError("زمان استفاده از کد تخفیف شروع نشده است")
    if _utc_value(row.ends_at) and _utc_value(row.ends_at) < now:
        raise ValueError("کد تخفیف منقضی شده است")
    if row.max_uses is not None and row.used_count >= row.max_uses:
        raise ValueError("ظرفیت کد تخفیف تکمیل شده است")
    if row.min_amount_rial and amount_rial < row.min_amount_rial:
        raise ValueError("مبلغ سفارش به حداقل کد تخفیف نرسیده است")
    if row.new_customers_only and customer is None:
        raise ValueError("برای استفاده از این کد، ثبت شماره یا حساب مشتری الزامی است")
    if row.new_customers_only and customer and customer.purchase_count > 0:
        raise ValueError("این کد فقط برای مشتری جدید است")
    if row.discount_type == "fixed":
        discount = int(row.value)
    else:
        discount = amount_rial * max(0, min(100, int(row.value))) // 100
    if row.max_discount_rial:
        discount = min(discount, int(row.max_discount_rial))
    return row, max(0, min(discount, max(0, amount_rial - 10_000)))


async def resolve_affiliate_code(session: AsyncSession, merchant_id: int, code: str | None) -> Affiliate | None:
    normalized = (code or "").strip().upper()
    if not normalized:
        return None
    row = await session.scalar(select(Affiliate).where(
        Affiliate.merchant_id == merchant_id,
        func.upper(Affiliate.code) == normalized,
        Affiliate.is_active.is_(True),
    ).limit(1))
    if not row:
        raise ValueError("کد معرف معتبر نیست")
    return row


async def register_affiliate_commission(session: AsyncSession, invoice: Invoice) -> AffiliateCommission | None:
    if not invoice.affiliate_id:
        return None
    existing = await session.scalar(select(AffiliateCommission).where(
        AffiliateCommission.affiliate_id == invoice.affiliate_id,
        AffiliateCommission.invoice_id == invoice.id,
    ))
    if existing:
        return existing
    affiliate = await session.get(Affiliate, invoice.affiliate_id)
    if not affiliate or not affiliate.is_active:
        return None
    if affiliate.commission_type == "fixed":
        amount = int(affiliate.commission_value)
    else:
        amount = invoice.base_amount_rial * max(0, min(100, int(affiliate.commission_value))) // 100
    amount = max(0, amount)
    row = AffiliateCommission(
        merchant_id=invoice.merchant_id,
        affiliate_id=affiliate.id,
        invoice_id=invoice.id,
        amount_rial=amount,
        status="approved",
        approved_at=utcnow(),
    )
    affiliate.wallet_balance_rial += amount
    session.add(row)
    return row


async def create_invoice_from_template(
    session: AsyncSession,
    template: InvoiceTemplate,
    *,
    customer_id: int | None = None,
    source_channel: str = "template",
    subscription_id: int | None = None,
) -> Invoice:
    merchant = await session.get(Merchant, template.merchant_id)
    if not merchant or not template.is_active or not template.amount_rial:
        raise ValueError("قالب فاکتور فعال یا کامل نیست")
    settings_data = json_loads(template.settings_json, {})
    store = await session.get(Store, template.store_id) if template.store_id else None
    invoice = await create_invoice(
        session,
        merchant=merchant,
        base_amount_rial=int(template.amount_rial),
        description=template.description or template.name,
        order_id=f"TPL-{template.id}-{secrets.token_hex(5).upper()}",
        fee_mode=template.fee_mode,
        card_id=template.card_id,
        ttl_minutes=max(5, min(1440, int(template.ttl_minutes or 30))),
        store_id=template.store_id,
        callback_url=store.callback_url if store else merchant.callback_url,
        callback_secret=store.callback_secret if store else merchant.callback_secret,
        return_url=settings_data.get("return_url") or merchant.return_url,
        source_channel=source_channel,
    )
    invoice.customer_id = customer_id
    invoice.subscription_id = subscription_id
    invoice.completion_mode = str(settings_data.get("completion_mode") or "exact")[:20]
    return invoice


async def dispatch_payment_request(session: AsyncSession, request_row: PaymentRequest) -> Invoice:
    if request_row.status not in {"draft", "queued", "failed"}:
        if request_row.invoice_id:
            existing = await session.get(Invoice, request_row.invoice_id)
            if existing:
                return existing
        raise ValueError("این درخواست پرداخت قبلاً ارسال شده است")
    merchant = await session.get(Merchant, request_row.merchant_id)
    if not merchant:
        raise ValueError("پذیرنده یافت نشد")
    due_at = _utc_value(request_row.due_at)
    ttl_minutes = max(5, int((due_at - utcnow()).total_seconds() // 60)) if due_at else 30
    invoice = await create_invoice(
        session,
        merchant=merchant,
        base_amount_rial=request_row.amount_rial,
        description=request_row.description or "درخواست پرداخت",
        order_id=f"REQ-{request_row.id}-{secrets.token_hex(4).upper()}",
        ttl_minutes=ttl_minutes,
        source_channel="payment_request",
    )
    invoice.customer_id = request_row.customer_id
    request_row.invoice_id = invoice.id
    request_row.status = "sent"
    customer = await session.get(Customer, request_row.customer_id) if request_row.customer_id else None
    target = request_row.delivery_target or (str(customer.telegram_user_id) if customer and customer.telegram_user_id else None)
    session.add(FulfillmentJob(
        merchant_id=request_row.merchant_id,
        invoice_id=invoice.id,
        customer_id=request_row.customer_id,
        action_type="telegram" if request_row.delivery_channel == "telegram" else "webhook",
        payload_json=json_dumps({"action": {
            "telegram_user_id": target,
            "url": target if request_row.delivery_channel == "webhook" else None,
            "text": f"درخواست پرداخت: {request_row.description or 'سفارش'}\\n{settings.base_url}/pay/{invoice.token}",
        }, "event": {"token": invoice.token, "payment_url": f"{settings.base_url}/pay/{invoice.token}"}}),
        status="pending", next_attempt_at=utcnow(), max_attempts=5,
    ))
    return invoice


async def process_commerce_schedules(session: AsyncSession, *, bot: Any = None, limit: int = 30) -> dict[str, int]:
    now = utcnow()
    counts = {"subscriptions": 0, "scheduled": 0, "reminders": 0, "requests": 0, "failed": 0}

    subscriptions = list((await session.scalars(select(Subscription).where(
        Subscription.status == "active", Subscription.next_invoice_at <= now
    ).order_by(Subscription.next_invoice_at.asc()).limit(limit))).all())
    for subscription in subscriptions:
        try:
            async with session.begin_nested():
                plan = await session.get(SubscriptionPlan, subscription.plan_id)
                customer = await session.get(Customer, subscription.customer_id)
                merchant = await session.get(Merchant, subscription.merchant_id)
                if not plan or not plan.is_active or not customer or not merchant:
                    subscription.status = "paused"
                    continue
                if not plan.auto_create_invoice:
                    subscription.next_invoice_at = now + timedelta(days=max(1, plan.interval_days))
                    continue
                invoice = await create_invoice(
                    session, merchant=merchant, base_amount_rial=plan.amount_rial,
                    description=f"تمدید {plan.name}", order_id=f"SUB-{subscription.id}-{subscription.current_cycle + 1}",
                    ttl_minutes=max(60, plan.grace_days * 1440), store_id=plan.store_id,
                    source_channel="subscription",
                )
                invoice.customer_id = customer.id
                invoice.subscription_id = subscription.id
                subscription.current_cycle += 1
                subscription.last_invoice_id = invoice.id
                subscription.next_invoice_at = now + timedelta(days=max(1, plan.interval_days))
                if plan.max_cycles and subscription.current_cycle >= plan.max_cycles:
                    subscription.status = "completed"
                session.add(PaymentReminder(
                    merchant_id=merchant.id, invoice_id=invoice.id, customer_id=customer.id,
                    scheduled_at=now, channel="telegram", status="pending",
                ))
                counts["subscriptions"] += 1
        except Exception as exc:
            counts["failed"] += 1
            await create_inbox_item(
                session, merchant_id=subscription.merchant_id, category="subscription_failed",
                severity="high", title=f"خطا در صدور اشتراک #{subscription.id}",
                detail=f"{type(exc).__name__}: {exc}"[:1500],
            )
            subscription.next_invoice_at = now + timedelta(minutes=15)

    schedules = list((await session.scalars(select(ScheduledInvoice).where(
        ScheduledInvoice.is_active.is_(True), ScheduledInvoice.next_run_at <= now
    ).order_by(ScheduledInvoice.next_run_at.asc()).limit(limit))).all())
    for schedule in schedules:
        try:
            async with session.begin_nested():
                template = await session.get(InvoiceTemplate, schedule.template_id)
                if not template or not template.is_active:
                    schedule.is_active = False
                    continue
                invoice = await create_invoice_from_template(session, template, customer_id=schedule.customer_id, source_channel="scheduled")
                session.add(PaymentReminder(
                    merchant_id=schedule.merchant_id, invoice_id=invoice.id, customer_id=schedule.customer_id,
                    scheduled_at=now, channel="telegram", status="pending",
                ))
                schedule.next_run_at = now + timedelta(days=max(1, schedule.interval_days))
                if schedule.remaining_runs is not None:
                    schedule.remaining_runs -= 1
                    if schedule.remaining_runs <= 0:
                        schedule.is_active = False
                counts["scheduled"] += 1
        except Exception as exc:
            counts["failed"] += 1
            await create_inbox_item(
                session, merchant_id=schedule.merchant_id, category="scheduled_invoice_failed",
                severity="high", title=f"خطا در فاکتور زمان‌بندی‌شده #{schedule.id}",
                detail=f"{type(exc).__name__}: {exc}"[:1500],
            )
            schedule.next_run_at = now + timedelta(minutes=15)

    requests = list((await session.scalars(select(PaymentRequest).where(
        PaymentRequest.status == "queued", or_(PaymentRequest.due_at.is_(None), PaymentRequest.due_at >= now)
    ).order_by(PaymentRequest.id.asc()).limit(limit))).all())
    for request_row in requests:
        try:
            async with session.begin_nested():
                await dispatch_payment_request(session, request_row)
                counts["requests"] += 1
        except Exception as exc:
            counts["failed"] += 1
            request_row.status = "failed"
            await create_inbox_item(
                session, merchant_id=request_row.merchant_id, category="payment_request_failed",
                severity="medium", title=f"ارسال درخواست پرداخت #{request_row.id} ناموفق بود",
                detail=f"{type(exc).__name__}: {exc}"[:1500],
            )

    expired_requests = list((await session.scalars(select(PaymentRequest).where(
        PaymentRequest.status == "queued", PaymentRequest.due_at.is_not(None), PaymentRequest.due_at < now
    ).limit(limit))).all())
    for request_row in expired_requests:
        request_row.status = "expired"

    reminders = list((await session.scalars(select(PaymentReminder).where(
        PaymentReminder.status == "pending", PaymentReminder.scheduled_at <= now
    ).order_by(PaymentReminder.scheduled_at.asc()).limit(limit))).all())
    for reminder in reminders:
        invoice = await session.get(Invoice, reminder.invoice_id)
        if not invoice or invoice.status not in {"pending", "partially_paid"}:
            reminder.status = "cancelled"
            continue
        customer = await session.get(Customer, reminder.customer_id) if reminder.customer_id else None
        reminder.attempt_count += 1
        try:
            if reminder.channel == "telegram" and bot is not None and customer and customer.telegram_user_id:
                remaining = max(0, invoice.payable_amount_rial - int(invoice.received_amount_rial or 0))
                await bot.send_message(customer.telegram_user_id, f"یادآوری پرداخت\nمبلغ باقی‌مانده: {remaining // 10:,} تومان\n{settings.base_url}/pay/{invoice.token}")
                reminder.status = "sent"
            else:
                reminder.status = "skipped"
                reminder.last_error = "کانال قابل ارسال یا مقصد مشتری تنظیم نشده است"
        except Exception as exc:
            reminder.status = "failed" if reminder.attempt_count >= 3 else "pending"
            reminder.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            if reminder.status == "pending":
                reminder.scheduled_at = now + timedelta(minutes=5)
            else:
                await create_inbox_item(
                    session, merchant_id=reminder.merchant_id, invoice_id=reminder.invoice_id,
                    category="reminder_failed", severity="medium", title="ارسال یادآوری پرداخت ناموفق بود",
                    detail=reminder.last_error,
                )
        counts["reminders"] += 1
    await session.flush()
    return counts


async def ensure_ordering_window(session: AsyncSession, merchant_id: int) -> None:
    profile = await ensure_option_profile(session, merchant_id)
    flags = json_loads(profile.feature_flags_json, {})
    config = flags.get("working_hours") or {}
    if not isinstance(config, dict) or not config.get("enabled"):
        if profile.emergency_mode:
            raise ValueError("صدور فاکتور توسط پذیرنده موقتاً متوقف شده است")
        return
    if profile.emergency_mode:
        raise ValueError("صدور فاکتور توسط پذیرنده موقتاً متوقف شده است")
    try:
        local_now = utcnow().astimezone(ZoneInfo(str(config.get("timezone") or profile.timezone or "Asia/Tehran")))
    except Exception:
        local_now = utcnow()
    day_rules = (config.get("days") or {}).get(str(local_now.weekday()), [])
    current = local_now.hour * 60 + local_now.minute
    allowed = False
    for window in day_rules if isinstance(day_rules, list) else []:
        try:
            start, end = window
            sh, sm = (int(x) for x in str(start).split(":", 1))
            eh, em = (int(x) for x in str(end).split(":", 1))
            begin, finish = sh * 60 + sm, eh * 60 + em
            allowed = begin <= current <= finish if begin <= finish else current >= begin or current <= finish
            if allowed:
                break
        except Exception:
            continue
    if not allowed:
        raise ValueError(str(config.get("outside_message") or "فروشگاه در حال حاضر خارج از ساعت کاری است"))


async def create_invoice_from_payment_link(
    session: AsyncSession,
    link: PaymentLink,
    *,
    amount_rial: int | None,
    customer_name: str | None,
    phone: str | None,
    order_id: str | None,
    source: str | None,
    session_id: str,
    discount_code: str | None = None,
    affiliate_code: str | None = None,
) -> Invoice:
    now = utcnow()
    if not link.is_active or (link.expires_at and link.expires_at < now):
        raise ValueError("این لینک پرداخت غیرفعال یا منقضی شده است")
    if link.max_uses is not None and link.used_count >= link.max_uses:
        raise ValueError("ظرفیت استفاده از این لینک تکمیل شده است")
    merchant = await session.get(Merchant, link.merchant_id)
    if not merchant:
        raise ValueError("پذیرنده یافت نشد")
    await ensure_ordering_window(session, merchant.id)
    product = await session.get(Product, link.product_id) if link.product_id else None
    resolved_amount = link.fixed_amount_rial or (product.price_rial if product else amount_rial)
    if not resolved_amount:
        raise ValueError("مبلغ پرداخت وارد نشده است")
    if link.min_amount_rial and resolved_amount < link.min_amount_rial:
        raise ValueError("مبلغ از حداقل لینک کمتر است")
    if link.max_amount_rial and resolved_amount > link.max_amount_rial:
        raise ValueError("مبلغ از حداکثر لینک بیشتر است")

    customer: Customer | None = None
    if customer_name or phone:
        normalized_phone = re.sub(r"\D", "", phone or "")
        if normalized_phone:
            candidates = list((await session.scalars(select(Customer).where(Customer.merchant_id == merchant.id, Customer.phone_last4 == normalized_phone[-4:]))).all())
            key = await get_setting(session, "encryption_key") or ""
            for item in candidates:
                try:
                    if item.phone_encrypted and re.sub(r"\D", "", decrypt_text(item.phone_encrypted, key)) == normalized_phone:
                        customer = item
                        break
                except Exception:
                    continue
        if not customer:
            customer, _ = await create_customer(session, merchant.id, name=customer_name or "مشتری لینک پرداخت", phone=phone)

    discount, discount_amount = await resolve_discount_code(
        session, merchant_id=merchant.id, store_id=link.store_id, code=discount_code,
        amount_rial=int(resolved_amount), customer=customer,
    )
    resolved_amount = max(10_000, int(resolved_amount) - discount_amount)
    affiliate = await resolve_affiliate_code(session, merchant.id, affiliate_code)

    experiment_id, variant_id, _variant_config = await select_ab_variant(session, merchant.id, link.store_id, session_id)
    store = await session.get(Store, link.store_id) if link.store_id else None
    invoice = await create_invoice(
        session,
        merchant=merchant,
        base_amount_rial=resolved_amount,
        description=link.description or (product.name if product else link.title),
        order_id=order_id or f"LINK-{link.id}-{secrets.token_hex(5).upper()}",
        fee_mode=link.fee_mode,
        ttl_minutes=link.ttl_minutes,
        store_id=link.store_id,
        callback_url=store.callback_url if store else None,
        callback_secret=store.callback_secret if store else None,
        source_channel=(source or "payment_link")[:40],
    )
    invoice.customer_id = customer.id if customer else None
    invoice.payment_link_id = link.id
    invoice.campaign_id = link.campaign_id
    invoice.completion_mode = link.completion_mode
    invoice.source_channel = (source or "payment_link")[:40]
    invoice.ab_variant_id = variant_id
    invoice.discount_id = discount.id if discount else None
    invoice.affiliate_id = affiliate.id if affiliate else None
    link.used_count += 1
    profile = await ensure_option_profile(session, merchant.id)
    flags = json_loads(profile.feature_flags_json, {})
    reminder_minutes = max(1, min(1440, int(flags.get("abandoned_payment_reminder_minutes") or 15)))
    session.add(PaymentReminder(
        merchant_id=merchant.id, invoice_id=invoice.id, customer_id=invoice.customer_id,
        scheduled_at=min(invoice.expires_at - timedelta(minutes=2), utcnow() + timedelta(minutes=reminder_minutes)),
        channel="telegram", status="pending",
    ))
    await record_analytics_event(
        session,
        merchant_id=merchant.id,
        store_id=link.store_id,
        invoice_id=invoice.id,
        payment_link_id=link.id,
        campaign_id=link.campaign_id,
        experiment_id=experiment_id,
        variant_id=variant_id,
        session_id=session_id,
        source=source,
        event_type="invoice.created",
    )
    return invoice


async def record_partial_payment(
    session: AsyncSession,
    invoice: Invoice,
    *,
    amount_rial: int,
    sms_id: int | None = None,
    source: str = "manual",
    reference_number: str | None = None,
    note: str | None = None,
) -> tuple[PartialPayment, Invoice]:
    if invoice.status not in {"pending", "partially_paid"}:
        raise ValueError("این فاکتور قابلیت دریافت پرداخت جدید ندارد")
    if invoice.completion_mode != "partial":
        raise ValueError("پرداخت چندتکه برای این فاکتور فعال نیست")
    if amount_rial <= 0:
        raise ValueError("مبلغ پرداخت باید بیشتر از صفر باشد")
    row = PartialPayment(
        invoice_id=invoice.id,
        sms_id=sms_id,
        amount_rial=amount_rial,
        source=source,
        status="accepted",
        reference_number=reference_number,
        received_at=utcnow(),
        note=note,
    )
    session.add(row)
    invoice.received_amount_rial = int(invoice.received_amount_rial or 0) + amount_rial
    if invoice.received_amount_rial < invoice.payable_amount_rial:
        invoice.status = "partially_paid"
        await record_payment_event(
            session,
            invoice,
            "payment.partial_received",
            status="partially_paid",
            actor_type=source,
            actor_id=sms_id,
            detail={
                "amount_rial": amount_rial,
                "received_amount_rial": invoice.received_amount_rial,
                "remaining_amount_rial": invoice.payable_amount_rial - invoice.received_amount_rial,
            },
        )
    else:
        overpaid = invoice.received_amount_rial - invoice.payable_amount_rial
        await session.flush()
        confirmed = await confirm_invoice_paid(session, invoice.id, sms_id, reference_number)
        if confirmed:
            invoice = confirmed
            if overpaid > 0:
                await create_inbox_item(
                    session,
                    merchant_id=invoice.merchant_id,
                    invoice_id=invoice.id,
                    category="overpayment",
                    severity="medium",
                    title="اضافه‌پرداخت فاکتور",
                    detail=f"مبلغ اضافه: {overpaid} ریال",
                )
    await session.flush()
    return row, invoice


async def create_refund_request(
    session: AsyncSession,
    invoice: Invoice,
    *,
    amount_rial: int,
    reason: str,
    destination: str | None,
    requested_by: str | None,
) -> RefundRequest:
    if invoice.status != "paid":
        raise ValueError("فقط فاکتور پرداخت‌شده قابل بازپرداخت است")
    if amount_rial <= 0 or amount_rial > invoice.received_amount_rial and amount_rial > invoice.payable_amount_rial:
        raise ValueError("مبلغ بازپرداخت معتبر نیست")
    encrypted = await encrypt_private_config(session, destination)
    row = RefundRequest(
        merchant_id=invoice.merchant_id,
        invoice_id=invoice.id,
        customer_id=invoice.customer_id,
        amount_rial=amount_rial,
        reason=reason,
        destination_encrypted=encrypted,
        requested_by=requested_by,
    )
    session.add(row)
    await create_inbox_item(
        session,
        merchant_id=invoice.merchant_id,
        invoice_id=invoice.id,
        category="refund",
        severity="high",
        title="درخواست بازپرداخت جدید",
        detail=reason,
    )
    return row


async def create_inbox_item(
    session: AsyncSession,
    *,
    category: str,
    title: str,
    merchant_id: int | None = None,
    invoice_id: int | None = None,
    severity: str = "medium",
    detail: str | None = None,
) -> AdminInboxItem:
    row = AdminInboxItem(
        merchant_id=merchant_id,
        invoice_id=invoice_id,
        category=category,
        severity=severity,
        title=title[:180],
        detail=detail,
    )
    session.add(row)
    return row


def conditions_match(conditions: dict[str, Any], payload: dict[str, Any]) -> bool:
    for key, expected in conditions.items():
        actual = payload.get(key)
        if key.endswith("_min"):
            source_key = key[:-4]
            if int(payload.get(source_key) or 0) < int(expected):
                return False
        elif key.endswith("_max"):
            source_key = key[:-4]
            if int(payload.get(source_key) or 0) > int(expected):
                return False
        elif isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


async def trigger_automations(
    session: AsyncSession,
    *,
    merchant_id: int,
    trigger: str,
    invoice: Invoice | None = None,
    store_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    data = dict(payload or {})
    if invoice:
        data.update({
            "invoice_id": invoice.id,
            "token": invoice.token,
            "status": invoice.status,
            "amount_rial": invoice.payable_amount_rial,
            "received_amount_rial": invoice.received_amount_rial,
            "store_id": invoice.store_id,
            "customer_id": invoice.customer_id,
            "source_channel": invoice.source_channel,
        })
        store_id = invoice.store_id
    rules = list((await session.scalars(
        select(AutomationRule)
        .where(
            AutomationRule.merchant_id == merchant_id,
            AutomationRule.trigger == trigger,
            AutomationRule.is_active.is_(True),
            or_(AutomationRule.store_id.is_(None), AutomationRule.store_id == store_id),
        )
        .order_by(AutomationRule.priority.asc(), AutomationRule.id.asc())
    )).all())
    queued = 0
    for rule in rules:
        if not conditions_match(json_loads(rule.conditions_json, {}), data):
            continue
        execution = AutomationExecution(
            rule_id=rule.id,
            merchant_id=merchant_id,
            invoice_id=invoice.id if invoice else None,
            trigger=trigger,
            status="queued",
            input_json=json_dumps(data),
        )
        session.add(execution)
        await session.flush()
        actions = json_loads(rule.actions_json, [])
        if not isinstance(actions, list):
            actions = []
        for action in actions:
            if not isinstance(action, dict) or not action.get("type"):
                continue
            session.add(FulfillmentJob(
                merchant_id=merchant_id,
                store_id=store_id,
                invoice_id=invoice.id if invoice else None,
                customer_id=invoice.customer_id if invoice else None,
                connector_id=action.get("connector_id"),
                action_type=str(action["type"])[:64],
                payload_json=json_dumps({"event": data, "action": action, "execution_id": execution.id}),
                status="pending",
                next_attempt_at=utcnow(),
            ))
            queued += 1
        execution.status = "queued" if actions else "skipped"
        execution.finished_at = utcnow() if not actions else None
        rule.run_count += 1
        rule.last_run_at = utcnow()

    subscriptions = list((await session.scalars(
        select(WebhookSubscription).where(
            WebhookSubscription.merchant_id == merchant_id,
            WebhookSubscription.event_type == trigger,
            WebhookSubscription.is_active.is_(True),
            or_(WebhookSubscription.store_id.is_(None), WebhookSubscription.store_id == store_id),
        )
    )).all())
    for sub in subscriptions:
        session.add(FulfillmentJob(
            merchant_id=merchant_id,
            store_id=store_id,
            invoice_id=invoice.id if invoice else None,
            customer_id=invoice.customer_id if invoice else None,
            action_type="webhook_subscription",
            payload_json=json_dumps({"event": data, "action": {"url": sub.url, "subscription_id": sub.id}}),
            status="pending",
            next_attempt_at=utcnow(),
        ))
        queued += 1
    return queued


async def process_fulfillment_jobs(session: AsyncSession, bot: Any | None = None, limit: int = 25) -> int:
    now = utcnow()
    jobs = list((await session.scalars(
        select(FulfillmentJob)
        .where(FulfillmentJob.status.in_(["pending", "retry"]), FulfillmentJob.next_attempt_at <= now)
        .order_by(FulfillmentJob.id.asc())
        .limit(limit)
    )).all())
    processed = 0
    for job in jobs:
        job.status = "processing"
        job.attempt_count += 1
        payload = json_loads(job.payload_json, {})
        action = payload.get("action") or {}
        event = payload.get("event") or {}
        try:
            if job.action_type in {"webhook", "webhook_subscription", "activate_service", "suspend_service", "connector", "n8n", "zapier", "make"}:
                url = action.get("url")
                connector = await session.get(IntegrationConnector, job.connector_id) if job.connector_id else None
                connector_config: dict[str, Any] = {}
                if connector:
                    connector_config = await decrypt_private_config(session, connector.config_encrypted)
                    base = connector.base_url or ""
                    endpoint = str(action.get("endpoint") or connector_config.get("endpoint") or "")
                    url = url or (urljoin(base.rstrip("/") + "/", endpoint.lstrip("/")) if endpoint else base)
                valid, normalized = validate_public_https_url(str(url or ""))
                if not valid:
                    raise ValueError(normalized)
                headers = {"User-Agent": "BluePay-Automation/1.2", "Content-Type": "application/json"}
                configured_headers = connector_config.get("headers") or {}
                if isinstance(configured_headers, dict):
                    headers.update({str(k)[:100]: str(v)[:1000] for k, v in configured_headers.items()})
                bearer = action.get("bearer_token") or connector_config.get("bearer_token") or connector_config.get("api_key")
                if bearer and "Authorization" not in headers:
                    headers["Authorization"] = f"Bearer {bearer}"
                secret = action.get("secret") or connector_config.get("signing_secret")
                body = json_dumps(event).encode()
                if secret:
                    headers["X-BluePay-Signature"] = hmac.new(str(secret).encode(), body, hashlib.sha256).hexdigest()
                method = str(action.get("method") or connector_config.get("method") or "POST").upper()
                if method not in {"POST", "PUT", "PATCH"}:
                    method = "POST"
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                    response = await client.request(method, normalized, content=body, headers=headers)
                if not 200 <= response.status_code < 300:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
                job.result_json = json_dumps({"http_status": response.status_code, "response": response.text[:500]})
            elif job.action_type in {"telegram", "message"}:
                if bot is None:
                    raise RuntimeError("Telegram bot is not available")
                target = action.get("telegram_user_id")
                if not target:
                    merchant = await session.get(Merchant, job.merchant_id)
                    target = merchant.telegram_user_id if merchant else None
                if not target:
                    raise ValueError("Telegram target is missing")
                text = str(action.get("text") or "رویداد جدید BluePay")
                for key, value in event.items():
                    text = text.replace("{" + str(key) + "}", str(value))
                await bot.send_message(int(target), text[:3900])
                job.result_json = json_dumps({"telegram_user_id": int(target)})
            elif job.action_type in {"issue_download", "digital_delivery"}:
                job.result_json = json_dumps({"delivery_token": secrets.token_urlsafe(24), "expires_in": 86400})
            elif job.action_type in {"create_ticket", "review"}:
                await create_inbox_item(
                    session,
                    merchant_id=job.merchant_id,
                    invoice_id=job.invoice_id,
                    category="automation",
                    title=str(action.get("title") or "اقدام اتوماسیون نیازمند بررسی"),
                    detail=json_dumps(event),
                )
                job.result_json = json_dumps({"inbox": True})
            elif job.action_type in {"customer_credit", "cashback", "loyalty_points"}:
                if not job.customer_id:
                    raise ValueError("Customer is missing")
                wallet = await session.scalar(select(CustomerWallet).where(CustomerWallet.merchant_id == job.merchant_id, CustomerWallet.customer_id == job.customer_id))
                if not wallet:
                    wallet = CustomerWallet(merchant_id=job.merchant_id, customer_id=job.customer_id)
                    session.add(wallet)
                    await session.flush()
                amount = int(action.get("amount_rial") or 0)
                points = int(action.get("points") or 0)
                wallet.balance_rial += amount
                wallet.points += points
                session.add(CustomerWalletEntry(
                    wallet_id=wallet.id,
                    entry_type=job.action_type,
                    amount_rial=amount,
                    points=points,
                    reference_type="invoice",
                    reference_id=str(job.invoice_id or ""),
                    description=str(action.get("description") or "اعتبار خودکار"),
                    idempotency_key=f"job:{job.id}",
                ))
                job.result_json = json_dumps({"balance_rial": wallet.balance_rial, "points": wallet.points})
            else:
                job.result_json = json_dumps({"accepted": True, "action_type": job.action_type})
            job.status = "completed"
            job.last_error = None
            processed += 1
        except Exception as exc:
            job.last_error = f"{type(exc).__name__}: {exc}"[:1500]
            if job.attempt_count >= job.max_attempts:
                job.status = "failed"
                await create_inbox_item(
                    session,
                    merchant_id=job.merchant_id,
                    invoice_id=job.invoice_id,
                    category="automation_failed",
                    severity="high",
                    title=f"شکست عملیات {job.action_type}",
                    detail=job.last_error,
                )
            else:
                job.status = "retry"
                job.next_attempt_at = now + timedelta(seconds=min(3600, 30 * (2 ** max(0, job.attempt_count - 1))))
    await session.flush()
    return processed


async def resolve_card_routing(
    session: AsyncSession,
    *,
    merchant_id: int,
    store_id: int | None,
    amount_rial: int,
    source_channel: str | None,
) -> int | None:
    rules = list((await session.scalars(
        select(CardRoutingRule)
        .where(
            CardRoutingRule.merchant_id == merchant_id,
            CardRoutingRule.is_active.is_(True),
            or_(CardRoutingRule.store_id.is_(None), CardRoutingRule.store_id == store_id),
        )
        .order_by(CardRoutingRule.priority.asc(), CardRoutingRule.id.asc())
    )).all())
    payload = {"amount_rial": amount_rial, "store_id": store_id, "source_channel": source_channel}
    for rule in rules:
        if conditions_match(json_loads(rule.conditions_json, {}), payload):
            card = await session.get(BankCard, rule.card_id)
            if card and card.is_active and card.merchant_id == merchant_id:
                return card.id
    return None


async def evaluate_dynamic_fraud_rules(
    session: AsyncSession,
    *,
    merchant_id: int,
    store_id: int | None,
    amount_rial: int,
    source_channel: str | None,
) -> tuple[int, str, list[str]]:
    rules = list((await session.scalars(
        select(FraudRule)
        .where(
            FraudRule.is_active.is_(True),
            or_(FraudRule.merchant_id.is_(None), FraudRule.merchant_id == merchant_id),
            or_(FraudRule.store_id.is_(None), FraudRule.store_id == store_id),
        )
        .order_by(FraudRule.id.asc())
    )).all())
    payload = {"amount_rial": amount_rial, "store_id": store_id, "source_channel": source_channel}
    score = 0
    actions: list[str] = []
    for rule in rules:
        if conditions_match(json_loads(rule.conditions_json, {}), payload):
            score += max(0, rule.score)
            actions.append(rule.action)
    if "block" in actions:
        return min(100, score), "blocked", actions
    if "review" in actions or score >= 50:
        return min(100, score), "review", actions
    return min(100, score), "approved", actions


async def on_invoice_paid_options(session: AsyncSession, invoice: Invoice) -> None:
    if invoice.received_amount_rial < invoice.payable_amount_rial:
        invoice.received_amount_rial = invoice.payable_amount_rial
    if invoice.customer_id:
        customer = await session.get(Customer, invoice.customer_id)
        if customer:
            customer.total_spend_rial += invoice.payable_amount_rial
            customer.purchase_count += 1
            customer.last_payment_at = utcnow()
    if invoice.ab_variant_id:
        variant = await session.get(AbVariant, invoice.ab_variant_id)
        if variant:
            variant.conversions += 1
    if invoice.discount_id:
        discount = await session.get(DiscountCode, invoice.discount_id)
        if discount:
            discount.used_count += 1
    if invoice.affiliate_id:
        await register_affiliate_commission(session, invoice)
    reminders = list((await session.scalars(select(PaymentReminder).where(
        PaymentReminder.invoice_id == invoice.id, PaymentReminder.status == "pending"
    ))).all())
    for reminder in reminders:
        reminder.status = "cancelled"
    if invoice.subscription_id:
        subscription = await session.get(Subscription, invoice.subscription_id)
        if subscription:
            subscription.status = "active"
    if invoice.payment_link_id:
        link = await session.get(PaymentLink, invoice.payment_link_id)
        product = await session.get(Product, link.product_id) if link and link.product_id else None
        if product:
            if product.inventory_count is not None:
                product.inventory_count = max(0, product.inventory_count - 1)
                if product.inventory_count == 0:
                    product.is_active = False
                    await create_inbox_item(
                        session, merchant_id=invoice.merchant_id, invoice_id=invoice.id,
                        category="inventory", severity="high", title=f"موجودی {product.name} تمام شد",
                    )
            existing_job = await session.scalar(select(FulfillmentJob).where(
                FulfillmentJob.invoice_id == invoice.id,
                FulfillmentJob.action_type == product.fulfillment_type,
            ))
            if not existing_job and product.fulfillment_type != "manual":
                config = await decrypt_private_config(session, product.fulfillment_config_encrypted)
                session.add(FulfillmentJob(
                    merchant_id=invoice.merchant_id, store_id=invoice.store_id, invoice_id=invoice.id,
                    customer_id=invoice.customer_id, connector_id=config.get("connector_id"),
                    action_type=product.fulfillment_type,
                    payload_json=json_dumps({"event": {
                        "invoice_id": invoice.id, "token": invoice.token, "product_id": product.id,
                        "customer_id": invoice.customer_id, "amount_rial": invoice.payable_amount_rial,
                    }, "action": config}),
                    status="pending", next_attempt_at=utcnow(), max_attempts=5,
                ))
    await record_analytics_event(
        session,
        merchant_id=invoice.merchant_id,
        store_id=invoice.store_id,
        invoice_id=invoice.id,
        payment_link_id=invoice.payment_link_id,
        campaign_id=invoice.campaign_id,
        variant_id=invoice.ab_variant_id,
        event_type="payment.paid",
        source=invoice.source_channel,
    )
    await trigger_automations(
        session,
        merchant_id=invoice.merchant_id,
        trigger="payment.paid",
        invoice=invoice,
    )


async def customer_portal_dashboard(session: AsyncSession, customer: Customer) -> dict[str, Any]:
    invoices = list((await session.scalars(
        select(Invoice).where(Invoice.customer_id == customer.id).order_by(Invoice.id.desc()).limit(100)
    )).all())
    wallet = await session.scalar(select(CustomerWallet).where(CustomerWallet.customer_id == customer.id))
    refunds = list((await session.scalars(
        select(RefundRequest).where(RefundRequest.customer_id == customer.id).order_by(RefundRequest.id.desc()).limit(30)
    )).all())
    subscriptions = list((await session.scalars(
        select(Subscription).where(Subscription.customer_id == customer.id).order_by(Subscription.id.desc()).limit(30)
    )).all())
    tickets = list((await session.scalars(
        select(SupportTicket).where(SupportTicket.customer_id == customer.id).order_by(SupportTicket.id.desc()).limit(30)
    )).all())
    requests = list((await session.scalars(
        select(PaymentRequest).where(PaymentRequest.customer_id == customer.id).order_by(PaymentRequest.id.desc()).limit(30)
    )).all())
    return {"customer": customer, "invoices": invoices, "wallet": wallet, "refunds": refunds, "subscriptions": subscriptions, "tickets": tickets, "payment_requests": requests}


async def analytics_funnel(session: AsyncSession, merchant_id: int, days: int = 30) -> dict[str, int]:
    since = utcnow() - timedelta(days=max(1, min(days, 366)))
    rows = (await session.execute(
        select(AnalyticsEvent.event_type, func.count(AnalyticsEvent.id))
        .where(AnalyticsEvent.merchant_id == merchant_id, AnalyticsEvent.occurred_at >= since)
        .group_by(AnalyticsEvent.event_type)
    )).all()
    values = {str(event): int(count) for event, count in rows}
    return {
        "link_viewed": values.get("payment_link.viewed", 0),
        "invoice_created": values.get("invoice.created", 0),
        "payment_page_viewed": values.get("payment_page.viewed", 0),
        "payment_paid": values.get("payment.paid", 0),
    }
