from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import AuditLog, BankCard, CallbackEvent, Invoice, Merchant, MerchantTeamMember, PaymentEvent, ReconciliationCase, SmsDevice, SmsTransaction, Store, WalletLedger
from app.services.report_service import merchant_financial_breakdown, merchant_financial_summary


def portal_token(merchant: Merchant) -> str:
    # The token is revoked when the merchant callback secret or SMS token
    # version is rotated, without storing another secret in the database.
    raw = f"portal:{merchant.id}:{merchant.telegram_user_id}:{merchant.sms_token_version}:{merchant.callback_secret or '-'}".encode()
    return hmac.new(settings.effective_portal_secret.encode(), raw, hashlib.sha256).hexdigest()


def verify_portal_token(merchant: Merchant, token: str) -> bool:
    return hmac.compare_digest(portal_token(merchant), token)


def merchant_portal_url(merchant: Merchant) -> str:
    return f"{settings.base_url}/portal/{merchant.id}/{portal_token(merchant)}"


async def build_portal_dashboard(session: AsyncSession, merchant: Merchant) -> dict:
    now = datetime.now(timezone.utc)
    day = now - timedelta(hours=24)
    invoice_count = int(await session.scalar(select(func.count(Invoice.id)).where(Invoice.merchant_id == merchant.id)) or 0)
    paid_count = int(await session.scalar(select(func.count(Invoice.id)).where(Invoice.merchant_id == merchant.id, Invoice.status == "paid")) or 0)
    paid_24h = int(await session.scalar(select(func.count(Invoice.id)).where(Invoice.merchant_id == merchant.id, Invoice.status == "paid", Invoice.paid_at >= day)) or 0)
    paid_volume = int(await session.scalar(select(func.coalesce(func.sum(Invoice.payable_amount_rial), 0)).where(Invoice.merchant_id == merchant.id, Invoice.status == "paid")) or 0)
    stores = list((await session.scalars(select(Store).where(Store.merchant_id == merchant.id).order_by(Store.id.desc()))).all())
    cards = list((await session.scalars(select(BankCard).where(BankCard.merchant_id == merchant.id).order_by(BankCard.is_default.desc(), BankCard.id.desc()))).all())
    invoices = list((await session.scalars(select(Invoice).where(Invoice.merchant_id == merchant.id).order_by(Invoice.id.desc()).limit(30))).all())
    ledger = list((await session.scalars(select(WalletLedger).where(WalletLedger.merchant_id == merchant.id).order_by(WalletLedger.id.desc()).limit(30))).all())
    sms = list((await session.scalars(select(SmsTransaction).join(Invoice, SmsTransaction.matched_invoice_id == Invoice.id).where(Invoice.merchant_id == merchant.id).order_by(SmsTransaction.id.desc()).limit(25))).all())
    cases = list((await session.scalars(select(ReconciliationCase).where(ReconciliationCase.merchant_id == merchant.id, ReconciliationCase.status == "open").order_by(ReconciliationCase.id.desc()).limit(30))).all())
    callbacks = list((await session.scalars(select(CallbackEvent).where(CallbackEvent.merchant_id == merchant.id).order_by(CallbackEvent.id.desc()).limit(30))).all())
    audits = list((await session.scalars(select(AuditLog).where(AuditLog.merchant_id == merchant.id).order_by(AuditLog.id.desc()).limit(30))).all())
    team = list((await session.scalars(select(MerchantTeamMember).where(MerchantTeamMember.merchant_id == merchant.id).order_by(MerchantTeamMember.is_active.desc(), MerchantTeamMember.id.asc()))).all())
    devices = list((await session.scalars(select(SmsDevice).where(SmsDevice.merchant_id == merchant.id).order_by(SmsDevice.is_active.desc(), SmsDevice.id.asc()))).all())
    timeline = list((await session.scalars(select(PaymentEvent).where(PaymentEvent.merchant_id == merchant.id).order_by(PaymentEvent.id.desc()).limit(40))).all())
    financial_summary = await merchant_financial_summary(session, merchant.id, days=30)
    financial_breakdown = await merchant_financial_breakdown(session, merchant, days=366)
    fee_30d = financial_summary["fee_rial"]
    gross_30d = financial_summary["gross_rial"]
    return {
        "invoice_count": invoice_count,
        "paid_count": paid_count,
        "paid_24h": paid_24h,
        "paid_volume_rial": paid_volume,
        "stores": stores,
        "cards": cards,
        "invoices": invoices,
        "ledger": ledger,
        "sms": sms,
        "cases": cases,
        "callbacks": callbacks,
        "audits": audits,
        "team": team,
        "devices": devices,
        "timeline": timeline,
        "fee_30d_rial": fee_30d,
        "gross_30d_rial": gross_30d,
        "financial_summary": financial_summary,
        "financial_breakdown": financial_breakdown,
    }
