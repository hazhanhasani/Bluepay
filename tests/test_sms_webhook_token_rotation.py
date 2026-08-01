from app.models import Merchant
from app.services.integration_service import merchant_sms_token, rotate_merchant_sms_token


def test_sms_token_is_stable_until_explicit_rotation():
    merchant = Merchant(id=7, telegram_user_id=1001, callback_secret="root-secret", sms_token_version=1)
    before = merchant_sms_token(merchant)
    assert before == merchant_sms_token(merchant)
    version = rotate_merchant_sms_token(merchant)
    after = merchant_sms_token(merchant)
    assert version == 2
    assert after != before


def test_sms_rotation_does_not_change_callback_secret():
    merchant = Merchant(id=8, telegram_user_id=1002, callback_secret="callback-secret", sms_token_version=1)
    rotate_merchant_sms_token(merchant)
    assert merchant.callback_secret == "callback-secret"
