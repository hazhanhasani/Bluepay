from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Merchant, MerchantTeamMember

VALID_TEAM_ROLES = {"owner", "finance", "developer", "support", "viewer"}
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {"*"},
    "finance": {"dashboard.view", "invoices.view", "wallet.view", "cards.view", "reports.export", "reconciliation.view"},
    "developer": {"dashboard.view", "api.manage", "callback.manage", "callbacks.retry", "invoices.view", "timeline.view"},
    "support": {"dashboard.view", "invoices.view", "timeline.view", "reconciliation.manage", "callbacks.retry"},
    "viewer": {"dashboard.view", "invoices.view", "timeline.view"},
}


def role_label(role: str) -> str:
    return {
        "owner": "مالک",
        "finance": "مدیر مالی",
        "developer": "توسعه‌دهنده",
        "support": "پشتیبان",
        "viewer": "فقط مشاهده",
    }.get(role, role)


def role_has_permission(role: str, permission: str) -> bool:
    permissions = ROLE_PERMISSIONS.get(role, set())
    return "*" in permissions or permission in permissions


async def add_team_member(
    session: AsyncSession,
    merchant: Merchant,
    telegram_user_id: int,
    role: str,
    *,
    invited_by: int | None,
) -> MerchantTeamMember:
    role = role.strip().lower()
    if role not in VALID_TEAM_ROLES - {"owner"}:
        raise ValueError("نقش معتبر نیست؛ finance، developer، support یا viewer مجاز است")
    if telegram_user_id == merchant.telegram_user_id:
        raise ValueError("مالک اصلی از قبل دسترسی کامل دارد")
    row = await session.scalar(
        select(MerchantTeamMember).where(
            MerchantTeamMember.merchant_id == merchant.id,
            MerchantTeamMember.telegram_user_id == telegram_user_id,
        )
    )
    if row:
        row.role = role
        row.is_active = True
        row.invited_by_telegram_user_id = invited_by
        return row
    row = MerchantTeamMember(
        merchant_id=merchant.id,
        telegram_user_id=telegram_user_id,
        role=role,
        is_active=True,
        invited_by_telegram_user_id=invited_by,
    )
    session.add(row)
    await session.flush()
    return row


async def list_team_members(session: AsyncSession, merchant_id: int) -> list[MerchantTeamMember]:
    return list(
        (
            await session.scalars(
                select(MerchantTeamMember)
                .where(MerchantTeamMember.merchant_id == merchant_id)
                .order_by(MerchantTeamMember.is_active.desc(), MerchantTeamMember.id.asc())
            )
        ).all()
    )


async def remove_team_member(session: AsyncSession, merchant_id: int, telegram_user_id: int) -> bool:
    row = await session.scalar(
        select(MerchantTeamMember).where(
            MerchantTeamMember.merchant_id == merchant_id,
            MerchantTeamMember.telegram_user_id == telegram_user_id,
        )
    )
    if not row:
        return False
    row.is_active = False
    return True


async def resolve_team_access(
    session: AsyncSession,
    telegram_user_id: int,
    merchant_id: int | None = None,
) -> tuple[Merchant | None, str | None]:
    owner_query = select(Merchant).where(Merchant.telegram_user_id == telegram_user_id, Merchant.is_active.is_(True))
    if merchant_id is not None:
        owner_query = owner_query.where(Merchant.id == merchant_id)
    owner = await session.scalar(owner_query)
    if owner:
        return owner, "owner"
    query = (
        select(MerchantTeamMember, Merchant)
        .join(Merchant, Merchant.id == MerchantTeamMember.merchant_id)
        .where(
            MerchantTeamMember.telegram_user_id == telegram_user_id,
            MerchantTeamMember.is_active.is_(True),
            Merchant.is_active.is_(True),
        )
    )
    if merchant_id is not None:
        query = query.where(MerchantTeamMember.merchant_id == merchant_id)
    result = (await session.execute(query.limit(1))).first()
    if not result:
        return None, None
    member, merchant = result
    member.last_access_at = datetime.now(timezone.utc)
    return merchant, member.role


def team_portal_token(member: MerchantTeamMember) -> str:
    import hashlib
    import hmac
    from app.core.config import settings

    raw = f"team-portal:{member.id}:{member.merchant_id}:{member.telegram_user_id}:{member.role}:{int(member.is_active)}".encode()
    return hmac.new(settings.effective_portal_secret.encode(), raw, hashlib.sha256).hexdigest()


def verify_team_portal_token(member: MerchantTeamMember, token: str) -> bool:
    import hmac
    return member.is_active and hmac.compare_digest(team_portal_token(member), token)


def team_portal_url(member: MerchantTeamMember) -> str:
    from app.core.config import settings
    return f"{settings.base_url}/portal/team/{member.id}/{team_portal_token(member)}"
