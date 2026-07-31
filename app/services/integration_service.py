from __future__ import annotations

import hashlib
import hmac

from app.core.config import settings
from app.models import Merchant


def _derived_token(merchant: Merchant, purpose: str, length: int = 40) -> str:
    """Create a stable per-merchant token without storing another secret.

    callback_secret remains the root merchant secret. Purpose separation prevents
    a token exposed for one route from being reusable for another route.
    """
    secret = (merchant.callback_secret or "").encode("utf-8")
    message = f"{purpose}:{merchant.id}:{merchant.telegram_user_id}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()[:length]


def merchant_sms_token(merchant: Merchant) -> str:
    return _derived_token(merchant, "sms-webhook")


def merchant_docs_token(merchant: Merchant) -> str:
    return _derived_token(merchant, "developer-docs", length=32)


def merchant_sms_webhook_url(merchant: Merchant) -> str:
    return f"{settings.base_url}/webhooks/sms/{merchant.id}/{merchant_sms_token(merchant)}"


def merchant_docs_url(merchant: Merchant) -> str:
    return f"{settings.base_url}/developers/{merchant.id}/{merchant_docs_token(merchant)}"


def verify_merchant_token(merchant: Merchant, purpose: str, supplied: str) -> bool:
    expected = _derived_token(
        merchant,
        purpose,
        length=32 if purpose == "developer-docs" else 40,
    )
    return hmac.compare_digest(expected, supplied)
