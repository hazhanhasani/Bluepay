from pathlib import Path


ROOT = Path(__file__).parents[1]
STORE_SERVICE = ROOT / "app" / "services" / "store_service.py"
MIGRATIONS = ROOT / "app" / "services" / "migration_service.py"
INVOICE_SERVICE = ROOT / "app" / "services" / "invoice_service.py"
ROUTES = ROOT / "app" / "api" / "routes.py"
KEYBOARDS = ROOT / "app" / "bot" / "keyboards.py"
DOCS = ROOT / "app" / "templates" / "developers.html"


def test_store_policy_allows_only_one_api_key():
    text = STORE_SERVICE.read_text(encoding="utf-8")
    assert "MAX_API_KEYS_PER_STORE = 1" in text
    assert "هر فروشگاه فقط یک کلید API دارد" in text
    assert "async def rotate_store_api_key" in text
    assert ".values(is_active=False)" in text


def test_upgrade_revokes_extra_active_keys_without_deleting_history():
    text = MIGRATIONS.read_text(encoding="utf-8")
    assert "enforce one active live key per store" in text
    assert "UPDATE store_api_keys SET is_active = 0" in text
    assert "DELETE FROM store_api_keys" not in text
    assert "uq_store_one_active_api_key" in text


def test_store_callback_is_separate_and_has_no_merchant_fallback():
    invoice_text = INVOICE_SERVICE.read_text(encoding="utf-8")
    route_text = ROUTES.read_text(encoding="utf-8")
    assert "callback_url if store_id is not None" in invoice_text
    assert "callback_secret if store_id is not None" in invoice_text
    assert "store.callback_url if store else merchant.callback_url" in route_text
    assert "merchant.callback_secret if not store else None" in route_text


def test_bot_and_docs_describe_one_key_per_store():
    keyboard_text = KEYBOARDS.read_text(encoding="utf-8")
    docs_text = DOCS.read_text(encoding="utf-8")
    assert "بازنشانی کلید API" in keyboard_text
    assert "هر فروشگاه فقط یک API دارد" in docs_text
    assert "کلید قبلی را فوراً باطل" in docs_text
    assert "Callback اختصاصی" in docs_text
