from app.parsers import parse_bank_sms


def test_mellat_credit_rial():
    parsed = parse_bank_sms(
        "Bank Mellat",
        "واریز به کارت ****1234 مبلغ 2,010,000 ریال شماره پیگیری 998877",
    )
    assert parsed.is_credit is True
    assert parsed.amount_rial == 2_010_000
    assert parsed.card_last4 == "1234"
    assert parsed.bank_code == "mellat"


def test_debit_is_not_credit():
    parsed = parse_bank_sms("Melli", "برداشت از کارت ****1111 مبلغ 500,000 ریال")
    assert parsed.is_credit is False
