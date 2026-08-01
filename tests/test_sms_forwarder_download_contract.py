from pathlib import Path


def test_official_sms_forwarder_redirect_is_exposed():
    routes = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert '@router.get("/downloads/sms-forwarder"' in routes
    assert "play.google.com/store/apps/details?id=com.frzinapps.smsforward" in routes
    assert "RedirectResponse" in routes


def test_sms_webhook_menu_links_to_project_download_route():
    keyboards = Path("app/bot/keyboards.py").read_text(encoding="utf-8")
    assert "دریافت نسخه رسمی SMS Forwarder" in keyboards
    assert "/downloads/sms-forwarder" in keyboards


def test_docs_show_official_download_link():
    template = Path("app/templates/developers.html").read_text(encoding="utf-8")
    assert "sms_forwarder_download_url" in template
    assert "نسخه رسمی" in template
