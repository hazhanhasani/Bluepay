from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from cryptography.fernet import Fernet, InvalidToken


def random_secret(length: int = 48) -> str:
    return secrets.token_urlsafe(length)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def api_key() -> str:
    return "gw_" + secrets.token_urlsafe(32)


def normalize_fernet_key(value: str) -> bytes:
    try:
        raw = value.encode("ascii")
        Fernet(raw)
        return raw
    except Exception:
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)


def encrypt_text(value: str, key: str) -> str:
    return Fernet(normalize_fernet_key(key)).encrypt(value.encode()).decode()


def decrypt_text(value: str, key: str) -> str:
    try:
        return Fernet(normalize_fernet_key(key)).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Encrypted value cannot be decrypted") from exc


def callback_signature(payload: dict, secret: str) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
