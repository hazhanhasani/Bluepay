from pathlib import Path


def test_button_updates_are_explicit_and_polling_is_zero_config_default():
    main_source = Path("app/main.py").read_text(encoding="utf-8")
    config_source = Path("app/core/config.py").read_text(encoding="utf-8")
    assert 'resolved.update({"message", "callback_query"})' in main_source
    assert 'await bot.delete_webhook(drop_pending_updates=False)' in main_source
    assert 'await bot.get_webhook_info()' in main_source
    assert 'polling_timeout=20' in main_source
    assert 'Field(default="polling", alias="TELEGRAM_MODE")' in config_source
    assert 'return mode == "webhook"' in config_source


def test_explicit_webhook_failure_falls_back_immediately():
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'webhook_failures >= 1' in source
    assert 'await telegram_polling_worker()' in source


def test_health_exposes_telegram_delivery_state():
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert '"telegram_ok": runtime_status.telegram_ok' in source
    assert '"telegram_mode": runtime_status.telegram_mode' in source
