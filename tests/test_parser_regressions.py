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
