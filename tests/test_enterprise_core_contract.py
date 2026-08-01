from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.db.base import Base
import app.models  # noqa: F401
from app.models import Store
from app.services.callback_outbox_service import CALLBACK_RETRY_AFTER_SECONDS, CALLBACK_TIMEOUT_SECONDS
from app.services.idempotency_service import canonical_request_hash
from app.services.risk_service import client_ip_allowed

ROOT = Path(__file__).parents[1]
ROUTES = (ROOT / "app" / "api" / "routes.py").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
PORTAL = (ROOT / "app" / "templates" / "portal.html").read_text(encoding="utf-8")


def test_enterprise_tables_create_on_clean_database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"callback_events", "callback_attempts", "sandbox_invoices", "idempotency_records", "audit_logs", "risk_events", "reconciliation_cases", "rate_limit_buckets"} <= tables


def test_callback_outbox_schedule_is_durable_contract():
    assert CALLBACK_RETRY_AFTER_SECONDS == (0, 30, 300)
    assert CALLBACK_TIMEOUT_SECONDS == 10
    assert "callback_outbox_worker" in MAIN
    assert "process_callback_outbox_batch" in MAIN


def test_live_and_sandbox_idempotency_routes_exist():
    assert 'Header(default=None, alias="Idempotency-Key")' in ROUTES
    assert '@router.post("/api/v1/sandbox/invoices")' in ROUTES
    assert '@router.post("/api/v1/sandbox/invoices/{token}/simulate")' in ROUTES
    assert "Idempotency-Replayed" in ROUTES


def test_idempotency_hash_is_stable_across_key_order():
    assert canonical_request_hash({"a": 1, "b": 2}) == canonical_request_hash({"b": 2, "a": 1})


def test_store_ip_allowlist_accepts_networks():
    store = Store(merchant_id=1, code="ST-X", name="X", callback_secret="s", allowed_ips="203.0.113.10,198.51.100.0/24")
    assert client_ip_allowed(store, "203.0.113.10") is True
    assert client_ip_allowed(store, "198.51.100.23") is True
    assert client_ip_allowed(store, "192.0.2.1") is False


def test_portal_contains_financial_and_operational_sections():
    for label in ("گردش کیف پول", "Callbackها", "نیازمند بررسی", "Audit Log", "مدیریت فروشگاه‌ها"):
        assert label in PORTAL
