from app.services.sms_payload_service import _recover_sms_from_any_payload, _parse_text_sms_payload


REAL_SMS = """بلو
واریز پول
سیدهژان عزیز، 505,870 ریال به حساب شما نشست.
موجودی: 2,172,029 ریال
۲۰:۳۰
۱۴۰۵.۰۵.۰۹"""


def test_recover_combined_from_template():
    raw = "+"  # force a different raw candidate too
    data = {"unknown_field": "+989999987641 (Blu Bank بلوبانک)\n" + REAL_SMS}
    # Without From: this still recovers the message from an unknown field.
    sender, message = _recover_sms_from_any_payload(data, raw)
    assert message == data["unknown_field"] or REAL_SMS in message


def test_parse_from_template():
    payload = "From : +989999987641 (Blu Bank بلوبانک)\n" + REAL_SMS
    parsed = _parse_text_sms_payload(payload)
    assert parsed["sender"].startswith("+989999")
    assert "505,870 ریال" in parsed["message"]


def test_recover_url_encoded_bare_payload():
    from urllib.parse import quote_plus

    payload = "From : +989999987641 (Blu Bank بلوبانک)\n" + REAL_SMS
    encoded = quote_plus(payload)
    sender, message = _recover_sms_from_any_payload({}, encoded)
    assert sender and "Blu Bank" in sender
    assert message and "505,870 ریال" in message
