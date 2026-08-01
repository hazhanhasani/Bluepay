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
    version = int(getattr(merchant, "sms_token_version", 1) or 1)
    # Keep existing installations compatible until the owner explicitly rotates.
    purpose = "sms-webhook" if version <= 1 else f"sms-webhook:v{version}"
    return _derived_token(merchant, purpose)


def merchant_docs_token(merchant: Merchant) -> str:
    return _derived_token(merchant, "developer-docs", length=32)


def merchant_sms_webhook_url(merchant: Merchant) -> str:
    return f"{settings.base_url}/webhooks/sms/{merchant.id}/{merchant_sms_token(merchant)}"


def merchant_docs_url(merchant: Merchant | None = None) -> str:
    """Return the public documentation URL.

    Merchant credentials and identifiers are intentionally never embedded in a
    documentation link. Sensitive or account-specific integration data is
    available only inside the authenticated Telegram bot.
    """
    return f"{settings.base_url}/developers"


def verify_merchant_token(merchant: Merchant, purpose: str, supplied: str) -> bool:
    expected = _derived_token(
        merchant,
        purpose,
        length=32 if purpose == "developer-docs" else 40,
    )
    return hmac.compare_digest(expected, supplied)


def rotate_merchant_sms_token(merchant: Merchant) -> int:
    merchant.sms_token_version = max(1, int(getattr(merchant, "sms_token_version", 1) or 1)) + 1
    return merchant.sms_token_version
