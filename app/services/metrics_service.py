from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CallbackEvent, Invoice, Merchant, ReconciliationCase, SmsTransaction, Store


async def prometheus_metrics(session: AsyncSession) -> str:
    day = datetime.now(timezone.utc) - timedelta(hours=24)
    values = {
        "bluepay_merchants_active": int(await session.scalar(select(func.count(Merchant.id)).where(Merchant.is_active.is_(True))) or 0),
        "bluepay_stores_active": int(await session.scalar(select(func.count(Store.id)).where(Store.is_active.is_(True))) or 0),
        "bluepay_invoices_pending": int(await session.scalar(select(func.count(Invoice.id)).where(Invoice.status == "pending")) or 0),
        "bluepay_invoices_paid_24h": int(await session.scalar(select(func.count(Invoice.id)).where(Invoice.status == "paid", Invoice.paid_at >= day)) or 0),
        "bluepay_callbacks_failed": int(await session.scalar(select(func.count(CallbackEvent.id)).where(CallbackEvent.status == "failed")) or 0),
        "bluepay_callbacks_pending": int(await session.scalar(select(func.count(CallbackEvent.id)).where(CallbackEvent.status.in_(["pending", "retry", "processing"]))) or 0),
        "bluepay_sms_review": int(await session.scalar(select(func.count(SmsTransaction.id)).where(SmsTransaction.status.in_(["review", "unmatched"]))) or 0),
        "bluepay_reconciliation_open": int(await session.scalar(select(func.count(ReconciliationCase.id)).where(ReconciliationCase.status == "open")) or 0),
    }
    lines = ["# BluePay operational metrics"]
    for key, value in values.items():
        lines.append(f"# TYPE {key} gauge")
        lines.append(f"{key} {value}")
    return "\n".join(lines) + "\n"
