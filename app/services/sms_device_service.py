from __future__ import annotations

import hashlib
import hmac
import time
from base64 import urlsafe_b64encode
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Merchant, SmsDevice


def normalize_device_id(value: str) -> str:
    value = " ".join((value or "").strip().split())
    if not value or len(value) > 120:
        raise ValueError("device_id معتبر نیست")
    return value


def device_secret(merchant: Merchant, device: SmsDevice) -> str:
    raw = f"sms-device:{merchant.id}:{device.device_id}:{device.secret_version}:{merchant.sms_token_version}".encode()
    digest = hmac.new(settings.effective_portal_secret.encode(), raw, hashlib.sha256).digest()
    return urlsafe_b64encode(digest).decode().rstrip("=")


def build_sms_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    message = timestamp.encode() + b"." + raw_body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_sms_signature(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > settings.sms_hmac_max_age_seconds:
        return False
    expected = build_sms_signature(secret, timestamp, raw_body)
    return hmac.compare_digest(expected, signature or "")


async def register_sms_device(
    session: AsyncSession,
    merchant: Merchant,
    device_id: str,
    *,
    name: str | None = None,
    allowed_bank_codes: list[str] | None = None,
    require_hmac: bool = True,
) -> tuple[SmsDevice, str]:
    device_id = normalize_device_id(device_id)
    row = await session.scalar(
        select(SmsDevice).where(SmsDevice.merchant_id == merchant.id, SmsDevice.device_id == device_id)
    )
    banks = ",".join(sorted({x.strip().lower() for x in (allowed_bank_codes or []) if x.strip()})) or None
    if not row:
        row = SmsDevice(
            merchant_id=merchant.id,
            device_id=device_id,
            name=(name or device_id)[:120],
            allowed_bank_codes=banks,
            is_active=True,
            require_hmac=require_hmac,
        )
        session.add(row)
        await session.flush()
    else:
        row.name = (name or row.name or device_id)[:120]
        row.allowed_bank_codes = banks
        row.require_hmac = require_hmac
        row.is_active = True
    return row, device_secret(merchant, row)


async def rotate_sms_device_secret(session: AsyncSession, merchant: Merchant, row: SmsDevice) -> str:
    row.secret_version += 1
    row.is_active = True
    await session.flush()
    return device_secret(merchant, row)


async def list_sms_devices(session: AsyncSession, merchant_id: int) -> list[SmsDevice]:
    return list(
        (
            await session.scalars(
                select(SmsDevice)
                .where(SmsDevice.merchant_id == merchant_id)
                .order_by(SmsDevice.is_active.desc(), SmsDevice.id.asc())
            )
        ).all()
    )


async def authenticate_sms_device(
    session: AsyncSession,
    merchant: Merchant,
    *,
    device_id: str | None,
    raw_body: bytes,
    timestamp: str,
    signature: str,
    bank_code: str | None,
    ip_address: str | None,
) -> SmsDevice | None:
    if not device_id:
        return None
    row = await session.scalar(
        select(SmsDevice).where(
            SmsDevice.merchant_id == merchant.id,
            SmsDevice.device_id == device_id,
            SmsDevice.is_active.is_(True),
        )
    )
    if not row:
        return None
    if row.allowed_bank_codes:
        allowed = {x.strip().lower() for x in row.allowed_bank_codes.split(",") if x.strip()}
        if bank_code and allowed and bank_code.strip().lower() not in allowed:
            raise PermissionError("این بانک برای دستگاه مجاز نیست")
    if row.require_hmac and not verify_sms_signature(device_secret(merchant, row), timestamp, raw_body, signature):
        raise PermissionError("امضای HMAC دستگاه معتبر نیست یا زمان درخواست منقضی شده است")
    now = datetime.now(timezone.utc)
    row.last_seen_at = now
    row.last_seen_ip = ip_address
    row.request_count += 1
    if signature:
        row.last_signature_at = now
    return row
