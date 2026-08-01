from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice, RiskEvent, Store


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    score: int
    status: str
    rule_code: str
    detail: str


def client_ip_allowed(store: Store | None, client_ip: str | None) -> bool:
    if not store or not store.allowed_ips:
        return True
    if not client_ip:
        return False
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for raw in store.allowed_ips.replace(";", ",").split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if address in network:
            return True
    return False


async def evaluate_invoice_creation(
    session: AsyncSession,
    *,
    merchant_id: int,
    store: Store | None,
    amount_rial: int,
    client_ip: str | None,
) -> RiskDecision:
    if not client_ip_allowed(store, client_ip):
        decision = RiskDecision(False, 100, "blocked", "IP_NOT_ALLOWED", "IP درخواست در فهرست مجاز فروشگاه نیست")
        session.add(RiskEvent(merchant_id=merchant_id, store_id=store.id if store else None, rule_code=decision.rule_code, score=decision.score, action=decision.status, detail=decision.detail, ip_address=client_ip))
        return decision

    now = datetime.now(timezone.utc)
    minute_limit = store.invoice_rate_limit_per_minute if store and store.invoice_rate_limit_per_minute else 30
    recent_count = int(await session.scalar(
        select(func.count(Invoice.id)).where(
            Invoice.merchant_id == merchant_id,
            *([Invoice.store_id == store.id] if store else []),
            Invoice.created_at >= now - timedelta(minutes=1),
        )
    ) or 0)
    if recent_count >= minute_limit:
        decision = RiskDecision(False, 90, "blocked", "INVOICE_RATE_EXCEEDED", f"سقف {minute_limit} فاکتور در دقیقه رد شد")
        session.add(RiskEvent(merchant_id=merchant_id, store_id=store.id if store else None, rule_code=decision.rule_code, score=decision.score, action=decision.status, detail=decision.detail, ip_address=client_ip))
        return decision

    daily_limit = store.daily_amount_limit_rial if store and store.daily_amount_limit_rial else 5_000_000_000
    start_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_amount = int(await session.scalar(
        select(func.coalesce(func.sum(Invoice.base_amount_rial), 0)).where(
            Invoice.merchant_id == merchant_id,
            *([Invoice.store_id == store.id] if store else []),
            Invoice.created_at >= start_day,
            Invoice.status.in_(["pending", "paid"]),
        )
    ) or 0)
    if daily_amount + amount_rial > daily_limit:
        decision = RiskDecision(False, 95, "blocked", "DAILY_AMOUNT_EXCEEDED", "سقف مبلغ روزانه فروشگاه رد شد")
        session.add(RiskEvent(merchant_id=merchant_id, store_id=store.id if store else None, rule_code=decision.rule_code, score=decision.score, action=decision.status, detail=decision.detail, ip_address=client_ip))
        return decision

    score = 0
    detail = "کنترل‌های نرخ، سقف روزانه و IP با موفقیت انجام شد"
    if amount_rial >= daily_limit // 4:
        score = 35
        detail = "مبلغ بالا است اما از سقف تعریف‌شده عبور نکرد"
    decision = RiskDecision(True, score, "approved" if score < 50 else "review", "RISK_OK", detail)
    if score:
        session.add(RiskEvent(merchant_id=merchant_id, store_id=store.id if store else None, rule_code=decision.rule_code, score=score, action=decision.status, detail=detail, ip_address=client_ip))
    return decision
