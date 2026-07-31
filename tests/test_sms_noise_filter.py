from app.services.sms_service import should_surface_unconfirmed_sms


def test_support_ticket_is_silent():
    message = "سید هژان عزیز، به تیکت با موضوع سفارش جدید پاسخ دادیم. برای چک کردنش به پنل کاربری مراجعه کن."
    assert should_surface_unconfirmed_sms(message, "generic", None) is False


def test_otp_is_silent():
    assert should_surface_unconfirmed_sms("رمز پویای شما 123456 است", "mellat", None) is False


def test_debit_is_silent():
    assert should_surface_unconfirmed_sms("خرید 500,000 ریال از کارت شما انجام شد", "blu", 500000) is False


def test_credit_like_message_is_surfaceable():
    assert should_surface_unconfirmed_sms("مبلغ 505,870 ریال به حساب شما واریز شد", "blu", 505870) is True
