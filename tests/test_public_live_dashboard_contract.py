from pathlib import Path


ROOT = Path(__file__).parents[1]
HOME = ROOT / "app" / "templates" / "home.html"
ROUTES = ROOT / "app" / "api" / "routes.py"
SERVICE = ROOT / "app" / "services" / "public_dashboard_service.py"
MODELS = ROOT / "app" / "models" / "entities.py"
CALLBACKS = ROOT / "app" / "services" / "callback_outbox_service.py"


def test_landing_reads_live_data_and_refreshes_without_cache():
    html = HOME.read_text(encoding="utf-8")
    routes = ROUTES.read_text(encoding="utf-8")
    assert 'id="live"' in html
    assert 'data-live="invoice-amount"' in html
    assert 'data-live="metric-paid"' in html
    assert 'setInterval' in html and '10000' in html
    assert '@router.get("/public/live"' in routes
    assert 'build_public_dashboard(session)' in routes
    assert '"Cache-Control": "no-store, max-age=0"' in routes


def test_public_snapshot_is_explicitly_anonymized():
    service = SERVICE.read_text(encoding="utf-8")
    assert "No merchant identity" in service
    assert '"card_mask"' in service
    assert "API key" in service
    forbidden_payload_fields = [
        '"merchant_name"',
        '"telegram_user_id"',
        '"order_id"',
        '"api_key"',
        '"callback_url"',
        '"sms_token"',
        '"reference_number"',
    ]
    for field in forbidden_payload_fields:
        assert field not in service


def test_callback_delivery_state_is_persisted_for_live_display():
    models = MODELS.read_text(encoding="utf-8")
    callbacks = CALLBACKS.read_text(encoding="utf-8")
    assert "callback_status" in models
    assert "callback_last_result" in models
    assert "callback_attempted_at" in models
    assert "CallbackEvent" in callbacks
    assert "CallbackAttempt" in callbacks
    assert 'event.status = "delivered"' in callbacks
    assert 'callback_status="delivered" if success' in callbacks
