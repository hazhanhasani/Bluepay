from app.parsers import BANK_PROFILES, normalize_bank_code, parse_bank_sms


def test_bank_catalog_is_comprehensive():
    assert len(BANK_PROFILES) >= 30
    required = {
        "melli", "sepah", "mellat", "tejarat", "saderat", "refah", "maskan",
        "keshavarzi", "sanat_madan", "postbank", "tosee_saderat", "tosee_taavon",
        "eghtesad_novin", "parsian", "karafarin", "saman", "pasargad", "sarmayeh",
        "sina", "shahr", "ayandeh", "gardeshgari", "dey", "iran_zamin",
        "khavarmianeh", "iran_venezuela", "mehr_iran", "resalat", "melal", "blu",
    }
    assert required.issubset({bank.code for bank in BANK_PROFILES})


def test_alias_normalization():
    assert normalize_bank_code("بانک ملی ایران") == "melli"
    assert normalize_bank_code("Bank Mellat") == "mellat"
    assert normalize_bank_code("بانک صادرات") == "saderat"
    assert normalize_bank_code("بانک قرض الحسنه رسالت") == "resalat"
    assert normalize_bank_code("بلوبانک") == "blu"
    assert normalize_bank_code("بانک انصار") == "sepah"


def test_parse_credit_with_bank_hint_toman():
    parsed = parse_bank_sms(
        "UNKNOWN-SENDER",
        "واریز به مبلغ ۲۵۰٬۰۰۰ تومان به کارت ****7281 شماره پیگیری 123456",
        bank_hint="ملت",
    )
    assert parsed.bank_code == "mellat"
    assert parsed.is_credit is True
    assert parsed.amount_rial == 2_500_000
    assert parsed.card_last4 == "7281"
    assert parsed.reference_number == "123456"
    assert parsed.confidence >= 65


def test_debit_is_never_credit():
    parsed = parse_bank_sms(
        "Bank Melli",
        "برداشت مبلغ 500,000 ریال از کارت ****1234 مانده 9,000,000 ریال",
    )
    assert parsed.bank_code == "melli"
    assert parsed.is_credit is False
    assert parsed.confidence <= 20


def test_balance_is_not_selected_as_amount():
    parsed = parse_bank_sms(
        "Bank Saderat",
        "واریز مبلغ 1,200,000 ریال به حساب ****7788 مانده 50,000,000 ریال",
    )
    assert parsed.amount_rial == 1_200_000
