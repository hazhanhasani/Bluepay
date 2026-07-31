from app.services.sms_service import sms_fingerprint


def test_same_sms_has_same_key_despite_transport_sender_changes():
    message = """بلو\nواریز پول\nمبلغ 1,009,440 ریال به حساب شما نشست.\nموجودی: 2,172,029 ریال"""
    a = sms_fingerprint(message, merchant_id=1, bank_code="blu", amount_rial=1_009_440)
    b = sms_fingerprint("  " + message.replace("\n", "  ") + "  ", merchant_id=1, bank_code="blubank", amount_rial=1_009_440)
    assert a == b


def test_different_merchant_or_amount_is_not_duplicate():
    message = "بلو واریز پول مبلغ 1,009,440 ریال به حساب شما نشست."
    base = sms_fingerprint(message, merchant_id=1, bank_code="blu", amount_rial=1_009_440)
    assert base != sms_fingerprint(message, merchant_id=2, bank_code="blu", amount_rial=1_009_440)
    assert base != sms_fingerprint(message, merchant_id=1, bank_code="blu", amount_rial=1_009_450)
