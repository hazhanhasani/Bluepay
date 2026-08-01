from __future__ import annotations

import csv
import html
import io
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice, Merchant, ReconciliationCase, Store, WalletLedger


def _bounded_days(days: int, maximum: int = 3660) -> int:
    return max(1, min(int(days), maximum))


def _spreadsheet_safe(value: object) -> object:
    """Prevent CSV/Excel formula injection for user-controlled strings."""

    if not isinstance(value, str):
        return value
    cleaned = value.replace("\x00", "").strip()
    if cleaned.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + cleaned
    return cleaned


async def merchant_financial_summary(session: AsyncSession, merchant_id: int, days: int = 30) -> dict:
    days = _bounded_days(days, 366)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    conditions = (Invoice.merchant_id == merchant_id, Invoice.status == "paid", Invoice.paid_at >= start)
    paid_count = int(await session.scalar(select(func.count(Invoice.id)).where(*conditions)) or 0)
    gross = int(await session.scalar(select(func.coalesce(func.sum(Invoice.payable_amount_rial), 0)).where(*conditions)) or 0)
    base = int(await session.scalar(select(func.coalesce(func.sum(Invoice.base_amount_rial), 0)).where(*conditions)) or 0)
    fees = int(await session.scalar(select(func.coalesce(func.sum(Invoice.fee_amount_rial), 0)).where(*conditions)) or 0)
    suspicious = int(
        await session.scalar(
            select(func.count(Invoice.id)).where(
                Invoice.merchant_id == merchant_id,
                Invoice.created_at >= start,
                (Invoice.risk_status != "approved") | (Invoice.risk_score >= 60),
            )
        )
        or 0
    )
    open_cases = int(
        await session.scalar(
            select(func.count(ReconciliationCase.id)).where(
                ReconciliationCase.merchant_id == merchant_id,
                ReconciliationCase.status == "open",
            )
        )
        or 0
    )
    return {
        "days": days,
        "paid_count": paid_count,
        "gross_rial": gross,
        "base_rial": base,
        "fee_rial": fees,
        "suspicious_count": suspicious,
        "open_reconciliation_count": open_cases,
    }


async def merchant_financial_breakdown(session: AsyncSession, merchant: Merchant, days: int = 366) -> dict:
    """Portable daily/monthly/store aggregation for SQLite and PostgreSQL."""

    days = _bounded_days(days, 3660)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        select(Invoice, Store.code, Store.name)
        .outerjoin(Store, Store.id == Invoice.store_id)
        .where(Invoice.merchant_id == merchant.id, Invoice.status == "paid", Invoice.paid_at >= start)
        .order_by(Invoice.paid_at.asc(), Invoice.id.asc())
    )
    rows = result.all()
    daily: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "gross_rial": 0, "base_rial": 0, "fee_rial": 0})
    monthly: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "gross_rial": 0, "base_rial": 0, "fee_rial": 0})
    stores: dict[str, dict[str, int | str]] = {}
    invoices: list[dict] = []

    for invoice, store_code, store_name in rows:
        paid_at = invoice.paid_at or invoice.created_at or datetime.now(timezone.utc)
        if paid_at.tzinfo is None:
            paid_at = paid_at.replace(tzinfo=timezone.utc)
        day_key = paid_at.date().isoformat()
        month_key = paid_at.strftime("%Y-%m")
        store_key = str(invoice.store_id or 0)
        store_bucket = stores.setdefault(
            store_key,
            {
                "store_id": invoice.store_id or 0,
                "store_code": store_code or "legacy",
                "store_name": store_name or "اتصال قدیمی",
                "count": 0,
                "gross_rial": 0,
                "base_rial": 0,
                "fee_rial": 0,
            },
        )
        for bucket in (daily[day_key], monthly[month_key], store_bucket):
            bucket["count"] = int(bucket["count"]) + 1
            bucket["gross_rial"] = int(bucket["gross_rial"]) + int(invoice.payable_amount_rial or 0)
            bucket["base_rial"] = int(bucket["base_rial"]) + int(invoice.base_amount_rial or 0)
            bucket["fee_rial"] = int(bucket["fee_rial"]) + int(invoice.fee_amount_rial or 0)
        invoices.append(
            {
                "id": invoice.id,
                "order_id": invoice.client_order_id or invoice.order_id,
                "store": store_name or store_code or "اتصال قدیمی",
                "base_amount_rial": int(invoice.base_amount_rial or 0),
                "payable_amount_rial": int(invoice.payable_amount_rial or 0),
                "fee_amount_rial": int(invoice.fee_amount_rial or 0),
                "reference_number": invoice.reference_number or "",
                "paid_at": paid_at.isoformat(),
                "callback_status": invoice.callback_status,
                "risk_status": invoice.risk_status,
                "risk_score": invoice.risk_score,
            }
        )

    def normalize(mapping: dict[str, dict], key_name: str) -> list[dict]:
        return [{key_name: key, **value} for key, value in sorted(mapping.items(), reverse=True)]

    return {
        "days": days,
        "daily": normalize(daily, "date"),
        "monthly": normalize(monthly, "month"),
        "stores": sorted(stores.values(), key=lambda item: int(item["gross_rial"]), reverse=True),
        "invoices": list(reversed(invoices)),
    }


async def invoices_csv(session: AsyncSession, merchant: Merchant, days: int = 90) -> bytes:
    start = datetime.now(timezone.utc) - timedelta(days=_bounded_days(days))
    result = await session.execute(
        select(Invoice, Store.code, Store.name)
        .outerjoin(Store, Store.id == Invoice.store_id)
        .where(Invoice.merchant_id == merchant.id, Invoice.created_at >= start)
        .order_by(Invoice.id.desc())
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "invoice_id", "order_id", "store_code", "store_name", "status", "base_amount_rial",
        "payable_amount_rial", "fee_amount_rial", "risk_status", "risk_score", "reference_number",
        "created_at", "paid_at", "callback_status",
    ])
    for row, store_code, store_name in result.all():
        writer.writerow([
            row.id, _spreadsheet_safe(row.client_order_id or row.order_id), _spreadsheet_safe(store_code or ""), _spreadsheet_safe(store_name or ""), row.status,
            row.base_amount_rial, row.payable_amount_rial, row.fee_amount_rial, row.risk_status, row.risk_score,
            _spreadsheet_safe(row.reference_number or ""), row.created_at.isoformat() if row.created_at else "",
            row.paid_at.isoformat() if row.paid_at else "", row.callback_status,
        ])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


async def wallet_csv(session: AsyncSession, merchant: Merchant, days: int = 365) -> bytes:
    start = datetime.now(timezone.utc) - timedelta(days=_bounded_days(days))
    rows = list(
        (
            await session.scalars(
                select(WalletLedger)
                .where(WalletLedger.merchant_id == merchant.id, WalletLedger.created_at >= start)
                .order_by(WalletLedger.id.desc())
            )
        ).all()
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ledger_id", "entry_type", "amount_rial", "balance_before_rial", "balance_after_rial",
        "reserved_before_rial", "reserved_after_rial", "reference_type", "reference_id", "description", "created_at",
    ])
    for row in rows:
        writer.writerow([
            row.id, row.entry_type, row.amount_rial, row.balance_before_rial, row.balance_after_rial,
            row.reserved_before_rial, row.reserved_after_rial, _spreadsheet_safe(row.reference_type or ""), _spreadsheet_safe(row.reference_id or ""),
            _spreadsheet_safe(row.description or ""), row.created_at.isoformat() if row.created_at else "",
        ])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def excel_compatible_statement(merchant: Merchant, summary: dict, breakdown: dict) -> bytes:
    """Create an Excel-readable multi-table .xls document without runtime dependencies."""

    def t(value: object) -> str:
        return html.escape(str(_spreadsheet_safe(value if value is not None else "")))

    def money(value: int) -> str:
        return f"{int(value) // 10:,}"

    def table(title: str, headers: list[str], data: list[list[object]]) -> str:
        head = "".join(f"<th>{t(item)}</th>" for item in headers)
        body = "".join("<tr>" + "".join(f"<td>{t(cell)}</td>" for cell in row) + "</tr>" for row in data)
        return f"<h2>{t(title)}</h2><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    summary_rows = [[
        summary["days"], summary["paid_count"], money(summary["gross_rial"]), money(summary["base_rial"]),
        money(summary["fee_rial"]), summary["suspicious_count"], summary["open_reconciliation_count"],
    ]]
    daily_rows = [[x["date"], x["count"], money(x["gross_rial"]), money(x["base_rial"]), money(x["fee_rial"])] for x in breakdown["daily"]]
    monthly_rows = [[x["month"], x["count"], money(x["gross_rial"]), money(x["base_rial"]), money(x["fee_rial"])] for x in breakdown["monthly"]]
    store_rows = [[x["store_code"], x["store_name"], x["count"], money(x["gross_rial"]), money(x["base_rial"]), money(x["fee_rial"])] for x in breakdown["stores"]]
    invoice_rows = [[x["id"], x["order_id"], x["store"], money(x["base_amount_rial"]), money(x["payable_amount_rial"]), money(x["fee_amount_rial"]), x["reference_number"], x["paid_at"], x["callback_status"], x["risk_status"], x["risk_score"]] for x in breakdown["invoices"]]
    document = f"""<!doctype html><html dir=\"rtl\"><head><meta charset=\"utf-8\"><style>
    body{{font-family:Tahoma,Arial;padding:20px}}h1{{color:#1459c7}}h2{{margin-top:28px;color:#17345e}}
    table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}th,td{{border:1px solid #b9c7db;padding:7px;text-align:center}}th{{background:#1459c7;color:white}}
    </style></head><body><h1>صورت‌حساب مالی بلوپی — {t(merchant.name)}</h1><p>شناسه پذیرنده: BP-{merchant.id:06d}</p>
    {table('خلاصه', ['بازه روز', 'تعداد پرداخت', 'مبلغ ناخالص تومان', 'مبلغ سفارش تومان', 'کارمزد تومان', 'مشکوک', 'مغایرت باز'], summary_rows)}
    {table('گزارش روزانه', ['تاریخ', 'تعداد', 'ناخالص تومان', 'سفارش تومان', 'کارمزد تومان'], daily_rows)}
    {table('گزارش ماهانه', ['ماه', 'تعداد', 'ناخالص تومان', 'سفارش تومان', 'کارمزد تومان'], monthly_rows)}
    {table('فروش هر فروشگاه', ['کد', 'فروشگاه', 'تعداد', 'ناخالص تومان', 'سفارش تومان', 'کارمزد تومان'], store_rows)}
    {table('تراکنش‌های پرداخت‌شده', ['ID', 'سفارش', 'فروشگاه', 'مبلغ سفارش', 'قابل پرداخت', 'کارمزد', 'پیگیری', 'زمان پرداخت', 'Callback', 'Risk', 'امتیاز'], invoice_rows)}
    </body></html>"""
    return ("\ufeff" + document).encode("utf-8")
