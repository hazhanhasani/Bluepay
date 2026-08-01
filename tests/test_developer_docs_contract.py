from pathlib import Path


ROOT = Path(__file__).parents[1]
DOCS = ROOT / "app" / "templates" / "developers.html"
ROUTES = ROOT / "app" / "api" / "routes.py"
MAIN = ROOT / "app" / "main.py"


def test_docs_cover_fee_modes_and_invoice_limits():
    text = DOCS.read_text(encoding="utf-8")
    for value in ("merchant", "customer", "split", "default", "free"):
        assert f"<code>{value}</code>" in text
    assert "مقدار مجاز API نیست" in text
    assert "حداقل ۱٬۰۰۰ و حداکثر ۵۰۰٬۰۰۰٬۰۰۰ تومان" in text
    assert "حداقل ۵ و حداکثر ۱٬۴۴۰ دقیقه" in text


def test_docs_cover_callback_schedule_rate_limit_sandbox_and_errors():
    text = DOCS.read_text(encoding="utf-8")
    assert "۲ ثانیه پس از پایان تلاش اول" in text
    assert "۶ ثانیه پس از پایان تلاش دوم" in text
    assert "Timeout هر تلاش ۱۲ ثانیه" in text
    assert "حداکثر ۱۲۰ درخواست در دقیقه" in text
    assert "حداکثر ۳۰ درخواست در دقیقه" in text
    assert "حداکثر ۳۰۰ درخواست در دقیقه" in text
    assert "X-RateLimit-Remaining" in text
    assert "Retry-After" in text
    assert "محیط Sandbox مستقل وجود ندارد" in text
    assert '"success": false' in text
    assert '"request_id": "req_' in text
    assert '"code": "SMS_TEMPLATE_NOT_RESOLVED"' in text


def test_docs_never_render_merchant_specific_data_or_live_sms_token():
    template = DOCS.read_text(encoding="utf-8")
    routes = ROUTES.read_text(encoding="utf-8")
    assert "اطلاعات اختصاصی پذیرنده" not in template
    assert "{{ merchant.name }}" not in template
    assert "{{ merchant.id }}" not in template
    assert "{{ api_prefix }}" not in template
    assert "{{ callback_url }}" not in template
    assert "{% if merchant %}" not in template
    assert "اطلاعات اختصاصی فقط داخل ربات احرازشده نمایش داده می‌شود" in template
    assert "توکن واقعی عمداً در این صفحه پنهان شده است" in template
    assert "تعویض توکن امنیتی وبهوک" in template
    legacy = routes.split('@router.get("/developers/{merchant_id}/{token}"', 1)[1].split('@router.get("/api/v1/banks")', 1)[0]
    assert "TemplateResponse" not in legacy
    assert "RedirectResponse" in legacy
    assert "merchant_sms_webhook_url" not in legacy
    assert "Cache-Control" in routes and "no-store" in routes
    assert "Referrer-Policy" in routes


def test_standard_error_contract_is_implemented_not_only_documented():
    text = MAIN.read_text(encoding="utf-8")
    assert 'code="RATE_LIMITED"' in text
    assert 'code = "INVALID_AMOUNT"' in text
    assert 'code = "INVALID_FEE_MODE"' in text
    assert 'X-Request-ID' in text
