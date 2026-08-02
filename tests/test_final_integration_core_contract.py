from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app/main.py").read_text(encoding="utf-8")
KEYBOARDS = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")


def test_telegram_uses_resilient_polling():
    block = MAIN.split("async def configure_telegram_delivery", 1)[1].split("async def lifespan", 1)[0]
    assert "telegram_polling_worker" in block
    assert "bot.set_webhook" not in block
    assert "polling-starting" in block


def test_manual_invoice_is_primary_and_options_are_hidden():
    assert "ساخت فاکتور دستی" in KEYBOARDS
    assert "مرکز آپشن" not in KEYBOARDS
    assert "اتوماسیون" not in KEYBOARDS
