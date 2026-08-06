from pathlib import Path


def test_button_updates_are_explicit_and_webhook_has_polling_fallback():
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'resolved.update({"message", "callback_query"})' in source
    assert 'await bot.get_webhook_info()' in source
    assert 'webhook_failures >= 3' in source
    assert 'await telegram_polling_worker()' in source


def test_health_exposes_telegram_delivery_state():
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert '"telegram_ok": runtime_status.telegram_ok' in source
    assert '"telegram_mode": runtime_status.telegram_mode' in source
