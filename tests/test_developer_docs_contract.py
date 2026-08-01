from pathlib import Path


DOCS = Path(__file__).parents[1] / "app" / "templates" / "developers.html"


def test_docs_cover_fee_modes_and_invoice_limits():
    text = DOCS.read_text(encoding="utf-8")
    for value in ("customer", "split", "merchant"):
        assert f"<code>{value}</code>" in text
    assert "حداقل ۱٬۰۰۰ و حداکثر ۵۰۰٬۰۰۰٬۰۰۰ تومان" in text
    assert "حداقل ۵ و حداکثر ۱٬۴۴۰ دقیقه" in text


def test_docs_cover_callback_schedule_rate_limit_sandbox_and_errors():
    text = DOCS.read_text(encoding="utf-8")
    assert "۲ ثانیه پس از پایان تلاش اول" in text
    assert "۶ ثانیه پس از پایان تلاش دوم" in text
    assert "Timeout هر تلاش ۱۲ ثانیه" in text
    assert "محدودیت ثابت در سطح برنامه اعمال نمی‌کند" in text
    assert "محیط Sandbox مستقل وجود ندارد" in text
    assert '"detail": "API key نامعتبر یا غیرفعال است"' in text
    assert '"error": "SMS_TEMPLATE_NOT_RESOLVED"' in text
