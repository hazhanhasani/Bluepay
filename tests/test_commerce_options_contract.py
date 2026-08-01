from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app/main.py").read_text(encoding="utf-8")
KEYBOARDS = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
HANDLERS = (ROOT / "app/bot/handlers.py").read_text(encoding="utf-8")
PORTAL = (ROOT / "app/templates/portal.html").read_text(encoding="utf-8")
DOCS = (ROOT / "app/templates/developers.html").read_text(encoding="utf-8")
INVOICE_SERVICE = (ROOT / "app/services/invoice_service.py").read_text(encoding="utf-8")
CALLBACK_SERVICE = (ROOT / "app/services/callback_outbox_service.py").read_text(encoding="utf-8")
ROUTES = (ROOT / "app/api/routes.py").read_text(encoding="utf-8")


def test_integration_focused_runtime_disables_commerce_options():
    assert "options_api_router" not in MAIN
    assert "options_bot_router" not in MAIN
    assert "fulfillment_worker" not in MAIN
    assert "commerce_scheduler_worker" not in MAIN
    assert "process_commerce_schedules" not in MAIN
    assert "trigger_automations" not in MAIN


def test_manual_invoice_remains_primary_user_flow():
    assert "ساخت فاکتور دستی" in KEYBOARDS
    assert 'callback_data="invoice:new"' in KEYBOARDS
    assert "ManualInvoiceState.amount" in HANDLERS
    assert "ManualInvoiceState.confirm" in HANDLERS
    assert "create_invoice(" in HANDLERS


def test_options_and_pos_are_not_exposed_in_active_ui():
    assert "مرکز آپشن‌ها و اتوماسیون" not in KEYBOARDS
    assert "/options" not in PORTAL
    assert "/pos" not in PORTAL
    assert "آپشن‌های تجاری و اتوماسیون" not in DOCS


def test_payment_and_callback_core_have_no_commerce_side_effects():
    assert "on_invoice_paid_options" not in INVOICE_SERVICE
    assert "trigger_automations" not in CALLBACK_SERVICE
    assert "create_inbox_item" not in CALLBACK_SERVICE
    assert "record_analytics_event" not in ROUTES
    assert "sms_automation_error" not in ROUTES
