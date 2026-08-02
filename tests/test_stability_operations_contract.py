from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_version_and_railway_readiness_contract():
    release = json.loads(read("release.json"))
    railway = json.loads(read("railway.json"))
    assert release["version"] == "1.2.2"
    assert release["run_migrations"] is True
    assert railway["deploy"]["healthcheckPath"] == "/ready"
    main = read("app/main.py")
    assert '@app.get("/health"' in main
    assert '@app.get("/ready"' in main
    assert '@app.post("/webhooks/telegram/{path_secret}"' in main
    assert "run_alembic_upgrade" in main
    assert "verify_database_and_schema" in main


def test_schema_guard_and_migration_contract():
    startup = read("app/services/startup_service.py")
    revision = read("alembic/versions/20260801_1100_stability_operations.py")
    migration = read("app/services/migration_service.py")
    assert "alembic" in startup.lower()
    assert "schema" in startup.lower()
    assert "Base.metadata.create_all" in revision
    assert '"return_url"' in revision
    assert "previous_commit_sha" in revision
    assert "ALTER TABLE merchants ADD COLUMN IF NOT EXISTS return_url" in migration


def test_team_device_timeline_and_reports_contract():
    models = read("app/models/entities.py")
    routes = read("app/api/routes.py")
    portal = read("app/templates/portal.html")
    for model in ("class PaymentEvent", "class MerchantTeamMember", "class SmsDevice"):
        assert model in models
    for path in (
        "/api/v1/account/sms-devices",
        "/api/v1/invoices/{token}/timeline",
        "/portal/team/{member_id}/{token}",
        "/reports/invoices.csv",
        "/reports/wallet.csv",
        "/reports/statement.xls",
        "/reports/statement",
        "/reports/service-invoice",
    ):
        assert path in routes
    assert "اعضای تیم پذیرنده" in portal
    assert "دستگاه‌های SMS Forwarder" in portal
    assert "خط زمانی" in portal
    assert "خروجی Excel" in portal
    assert "صورتحساب خدمات" in portal


def test_release_staging_rollback_and_github_permission_safety():
    service = read("app/services/github_service.py")
    admin = read("app/bot/admin.py")
    handlers = read("app/bot/handlers.py")
    assert "validate_release_zip" in service
    assert "previous_commit_sha" in service
    assert "async def rollback" in service
    assert "RELEASE_STAGING_BRANCH" in read("app/core/config.py")
    assert "rollback" in admin.lower()
    assert "package_sha256" in handlers
    assert not (ROOT / ".github" / "workflows").exists()
    assert (ROOT / "ops" / "ci-workflow.example.yml").exists()


def test_secure_sms_device_hmac_contract():
    service = read("app/services/sms_device_service.py")
    routes = read("app/api/routes.py")
    docs = read("app/templates/developers.html")
    assert "hmac" in service.lower()
    assert "sha256=" in service
    assert "X-BluePay-Timestamp" in routes
    assert "X-BluePay-Signature" in routes
    assert "X-BluePay-Signature" in docs


def test_sdk_timeline_and_sandbox_contract():
    py = read("sdks/python/bluepay.py")
    node = read("sdks/node/index.js")
    php = read("sdks/php/BluePay.php")
    assert "get_invoice_timeline" in py
    assert "getInvoiceTimeline" in node
    assert "getInvoiceTimeline" in php
    assert "simulate_sandbox" in py
    assert "simulateSandbox" in node
    assert "simulateSandbox" in php
