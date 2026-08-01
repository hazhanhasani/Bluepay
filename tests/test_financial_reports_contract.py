from __future__ import annotations

from types import SimpleNamespace

from app.services.report_service import excel_compatible_statement


def test_excel_compatible_statement_contains_financial_sections():
    merchant = SimpleNamespace(id=7, name="پذیرنده تست")
    summary = {
        "days": 30,
        "paid_count": 2,
        "gross_rial": 2_010_000,
        "base_rial": 2_000_000,
        "fee_rial": 10_000,
        "suspicious_count": 1,
        "open_reconciliation_count": 0,
    }
    breakdown = {
        "daily": [{"date": "2026-08-01", "count": 2, "gross_rial": 2_010_000, "base_rial": 2_000_000, "fee_rial": 10_000}],
        "monthly": [{"month": "2026-08", "count": 2, "gross_rial": 2_010_000, "base_rial": 2_000_000, "fee_rial": 10_000}],
        "stores": [{"store_code": "ST-1", "store_name": "اصلی", "count": 2, "gross_rial": 2_010_000, "base_rial": 2_000_000, "fee_rial": 10_000}],
        "invoices": [{"id": 1, "order_id": "=HYPERLINK(\"https://example.com\")", "store": "اصلی", "base_amount_rial": 1_000_000, "payable_amount_rial": 1_005_000, "fee_amount_rial": 5_000, "reference_number": "R-1", "paid_at": "2026-08-01T10:00:00+00:00", "callback_status": "delivered", "risk_status": "approved", "risk_score": 0}],
    }
    content = excel_compatible_statement(merchant, summary, breakdown)
    assert content.startswith(b"\xef\xbb\xbf")
    decoded = content.decode("utf-8-sig")
    assert "صورت‌حساب مالی بلوپی" in decoded
    assert "گزارش روزانه" in decoded
    assert "گزارش ماهانه" in decoded
    assert "فروش هر فروشگاه" in decoded
    assert "تراکنش‌های پرداخت‌شده" in decoded
    assert "&#x27;=HYPERLINK" in decoded
