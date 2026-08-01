from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ReconciliationCase


async def open_reconciliation_case(
    session: AsyncSession,
    *,
    case_key: str,
    case_type: str,
    detail: str,
    merchant_id: int | None = None,
    invoice_id: int | None = None,
    sms_id: int | None = None,
    severity: str = "medium",
) -> ReconciliationCase:
    row = await session.scalar(select(ReconciliationCase).where(ReconciliationCase.case_key == case_key))
    if row:
        if row.status == "resolved":
            row.status = "open"
            row.resolution = None
            row.resolved_at = None
            row.resolved_by = None
        row.detail = detail
        return row
    row = ReconciliationCase(
        case_key=case_key,
        case_type=case_type,
        detail=detail,
        merchant_id=merchant_id,
        invoice_id=invoice_id,
        sms_id=sms_id,
        severity=severity,
    )
    session.add(row)
    return row
