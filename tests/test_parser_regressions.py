from app.parsers import parse_bank_sms


def test_blu_sender_is_not_misdetected_as_dey():
    sender = "+989999987641 (Blu Bank بلوبانک)"
    message = """بلو
واریز پول
سیدمژان عزیز، 500,000 ریال به حساب شما نشست.
موجودی: 1,732,186 ریال
19:40
1405.05.09"""
    parsed = parse_bank_sms(sender, message)
    assert parsed.bank_code == "blu"
    assert parsed.is_credit is True
    assert parsed.amount_rial == 500_000
    assert parsed.confidence >= 65


def test_dey_full_sender_still_detects_dey():
    parsed = parse_bank_sms("Bank Dey بانک دی", "واریز مبلغ 500,000 ریال")
    assert parsed.bank_code == "dey"
    assert parsed.amount_rial == 500_000


def test_blu_exact_real_message_unique_amount():
    sender = "+989999987641 (Blu Bank بلوبانک)"
    message = """بلو
واریز پول
سیدهژان عزیز، 505,870 ریال به حساب شما نشست.
موجودی: 2,172,029 ریال
۲۰:۳۰
۱۴۰۵.۰۵.۰۹"""
    parsed = parse_bank_sms(sender, message)
    assert parsed.bank_code == "blu"
    assert parsed.is_credit is True
    assert parsed.amount_rial == 505_870
    assert parsed.confidence >= 65


def test_blu_body_without_heading_still_credit():
    parsed = parse_bank_sms(
        "unknown-forwarder",
        "سیدهژان عزیز، 505,870 ریال به حساب شما نشست. موجودی: 2,172,029 ریال",
    )
    assert parsed.is_credit is True
    assert parsed.amount_rial == 505_870
