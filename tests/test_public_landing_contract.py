from pathlib import Path


def test_public_landing_contains_product_sections():
    html = Path('app/templates/home.html').read_text(encoding='utf-8')
    required = [
        'id="how"',
        'id="features"',
        'id="developers"',
        'id="security"',
        'id="faq"',
        '/api/v1/invoices',
        '{{ telegram_url }}',
        '{{ docs_url }}',
    ]
    for value in required:
        assert value in html


def test_landing_does_not_expose_private_credentials():
    html = Path('app/templates/home.html').read_text(encoding='utf-8')
    forbidden = ['sms_webhook_token', 'callback_secret', 'merchant_id', 'bp_live_REAL']
    for value in forbidden:
        assert value not in html


def test_telegram_resolver_route_exists():
    routes = Path('app/api/routes.py').read_text(encoding='utf-8')
    assert '@router.get("/telegram"' in routes
    assert '/getMe' in routes
