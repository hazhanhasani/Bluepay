from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.datetime_utils import iso_utc_z, remaining_seconds


def test_payment_template_does_not_parse_naive_database_datetime():
    template = Path("app/templates/payment.html").read_text(encoding="utf-8")
    assert "invoice.expires_at.isoformat()" not in template
    assert "countdownRemainingMs" in template
    assert "data.remaining_seconds" in template


def test_routes_expose_server_authoritative_countdown():
    routes = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert '"remaining_seconds": remaining_seconds(expires_at, now=now)' in routes
    assert '"server_time": iso_utc_z(now)' in routes


def test_naive_sqlite_datetime_is_restored_as_utc():
    naive_utc = datetime(2026, 8, 6, 6, 0, 0)
    assert iso_utc_z(naive_utc) == "2026-08-06T06:00:00Z"
    assert remaining_seconds(
        naive_utc,
        now=datetime(2026, 8, 6, 5, 30, 0, tzinfo=timezone.utc),
    ) == 1800


def test_offset_datetime_is_serialized_as_z():
    iran_time = datetime(
        2026, 8, 6, 9, 30, 0,
        tzinfo=timezone(timedelta(hours=3, minutes=30)),
    )
    assert iso_utc_z(iran_time) == "2026-08-06T06:00:00Z"
