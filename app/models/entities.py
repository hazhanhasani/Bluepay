from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class Merchant(TimestampMixin, Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="پذیرنده")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    wallet_balance_rial: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_balance_rial: Mapped[int] = mapped_column(BigInteger, default=0)
    verification_fee_rial: Mapped[int] = mapped_column(BigInteger, default=20_000)
    fee_mode: Mapped[str] = mapped_column(String(20), default="merchant")

    api_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    api_key_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    return_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_secret: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sms_token_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    phone_number_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cards: Mapped[list[BankCard]] = relationship(back_populates="merchant", cascade="all, delete-orphan")
    invoices: Mapped[list[Invoice]] = relationship(back_populates="merchant", foreign_keys="Invoice.merchant_id")
    stores: Mapped[list[Store]] = relationship(back_populates="merchant", cascade="all, delete-orphan")

    @property
    def available_balance_rial(self) -> int:
        return self.wallet_balance_rial - self.reserved_balance_rial


class Store(TimestampMixin, Base):
    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("merchant_id", "code", name="uq_store_merchant_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    website_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_secret: Mapped[str] = mapped_column(String(160))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Optional risk/security policies. Empty means the global default applies.
    allowed_ips: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_amount_limit_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    merchant: Mapped[Merchant] = relationship(back_populates="stores")
    api_keys: Mapped[list[StoreApiKey]] = relationship(back_populates="store", cascade="all, delete-orphan")
    invoices: Mapped[list[Invoice]] = relationship(back_populates="store", foreign_keys="Invoice.store_id")


class StoreApiKey(TimestampMixin, Base):
    __tablename__ = "store_api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(80), default="کلید اصلی")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_legacy: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    store: Mapped[Store] = relationship(back_populates="api_keys")


class BankCard(TimestampMixin, Base):
    __tablename__ = "bank_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    bank_code: Mapped[str] = mapped_column(String(40), index=True)
    card_number_encrypted: Mapped[str] = mapped_column(Text)
    card_last4: Mapped[str] = mapped_column(String(4), index=True)
    account_holder: Mapped[str] = mapped_column(String(120))
    sms_source_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    daily_limit_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    merchant: Mapped[Merchant] = relationship(back_populates="cards")
    invoices: Mapped[list[Invoice]] = relationship(back_populates="card")


class Invoice(TimestampMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("merchant_id", "order_id", name="uq_invoice_order"),
        Index("uq_invoices_store_client_order", "store_id", "client_order_id", unique=True),
        Index("uq_invoices_store_idempotency", "store_id", "idempotency_key", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="RESTRICT"), index=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("bank_cards.id", ondelete="RESTRICT"), index=True)
    order_id: Mapped[str] = mapped_column(String(120), index=True)
    client_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    base_amount_rial: Mapped[int] = mapped_column(BigInteger)
    fee_amount_rial: Mapped[int] = mapped_column(BigInteger)
    customer_fee_rial: Mapped[int] = mapped_column(BigInteger)
    unique_amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    payable_amount_rial: Mapped[int] = mapped_column(BigInteger, index=True)
    fee_mode: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(30), default="payment", server_default="payment", index=True)
    environment: Mapped[str] = mapped_column(String(16), default="live", server_default="live", index=True)
    risk_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    risk_status: Mapped[str] = mapped_column(String(24), default="approved", server_default="approved", index=True)
    wallet_target_merchant_id: Mapped[int | None] = mapped_column(
        ForeignKey("merchants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    store_id: Mapped[int | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True
    )
    api_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("store_api_keys.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    payment_link_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    campaign_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    branch_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    received_amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    completion_mode: Mapped[str] = mapped_column(String(20), default="exact", server_default="exact", index=True)
    source_channel: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    ab_variant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    discount_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    affiliate_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    subscription_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    return_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_secret: Mapped[str | None] = mapped_column(String(160), nullable=True)
    callback_status: Mapped[str] = mapped_column(String(24), default="not_attempted", server_default="not_attempted", index=True)
    callback_last_result: Mapped[str | None] = mapped_column(String(160), nullable=True)
    callback_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    matched_sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_transactions.id"), nullable=True)
    reference_number: Mapped[str | None] = mapped_column(String(120), nullable=True)

    merchant: Mapped[Merchant] = relationship(back_populates="invoices", foreign_keys=[merchant_id])
    card: Mapped[BankCard] = relationship(back_populates="invoices")
    store: Mapped[Store | None] = relationship(back_populates="invoices", foreign_keys=[store_id])


class AmountReservation(TimestampMixin, Base):
    __tablename__ = "amount_reservations"
    __table_args__ = (UniqueConstraint("card_id", "payable_amount_rial", name="uq_active_card_payable_amount"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("bank_cards.id", ondelete="CASCADE"), index=True)
    invoice_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    payable_amount_rial: Mapped[int] = mapped_column(BigInteger, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class WalletLedger(TimestampMixin, Base):
    __tablename__ = "wallet_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True, index=True)
    entry_type: Mapped[str] = mapped_column(String(40), index=True)
    amount_rial: Mapped[int] = mapped_column(BigInteger)
    balance_before_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    balance_after_rial: Mapped[int] = mapped_column(BigInteger)
    reserved_before_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    reserved_after_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    reference_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    reference_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    reversed_entry_id: Mapped[int | None] = mapped_column(ForeignKey("wallet_ledger.id"), nullable=True, index=True)


class SmsTransaction(TimestampMixin, Base):
    __tablename__ = "sms_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    sender: Mapped[str] = mapped_column(String(120))
    device_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    raw_message: Mapped[str] = mapped_column(Text)
    bank_code: Mapped[str] = mapped_column(String(40), index=True)
    amount_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True, index=True)
    transaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reference_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    parse_confidence: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="received", index=True)
    matched_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)


class CallbackEvent(TimestampMixin, Base):
    __tablename__ = "callback_events"
    __table_args__ = (UniqueConstraint("event_key", name="uq_callback_event_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    environment: Mapped[str] = mapped_column(String(16), default="live", server_default="live", index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), nullable=True, index=True)
    sandbox_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("sandbox_invoices.id", ondelete="CASCADE"), nullable=True, index=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    callback_url: Mapped[str] = mapped_column(Text)
    callback_secret: Mapped[str] = mapped_column(String(160))
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_result: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delivery_id: Mapped[str] = mapped_column(String(64), index=True)


class CallbackAttempt(TimestampMixin, Base):
    __tablename__ = "callback_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("callback_events.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str] = mapped_column(String(200))
    response_preview: Mapped[str | None] = mapped_column(Text, nullable=True)


class IdempotencyRecord(TimestampMixin, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(120), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(30), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id", ondelete="SET NULL"), nullable=True, index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class RiskEvent(TimestampMixin, Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    rule_code: Mapped[str] = mapped_column(String(80), index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    action: Mapped[str] = mapped_column(String(24), index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SandboxInvoice(TimestampMixin, Base):
    __tablename__ = "sandbox_invoices"
    __table_args__ = (
        UniqueConstraint("store_id", "client_order_id", name="uq_sandbox_store_order"),
        UniqueConstraint("store_id", "idempotency_key", name="uq_sandbox_store_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    client_order_id: Mapped[str] = mapped_column(String(120), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_rial: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reference_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    callback_status: Mapped[str] = mapped_column(String(24), default="not_attempted", server_default="not_attempted", index=True)


class ReconciliationCase(TimestampMixin, Base):
    __tablename__ = "reconciliation_cases"
    __table_args__ = (UniqueConstraint("case_key", name="uq_reconciliation_case_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    case_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id", ondelete="SET NULL"), nullable=True, index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_transactions.id", ondelete="SET NULL"), nullable=True, index=True)
    case_type: Mapped[str] = mapped_column(String(60), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="medium", index=True)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RateLimitBucket(TimestampMixin, Base):
    __tablename__ = "rate_limit_buckets"
    __table_args__ = (UniqueConstraint("scope", "identity_hash", "window_start", name="uq_rate_limit_bucket"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(40), index=True)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)
    window_start: Mapped[int] = mapped_column(BigInteger, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")



class AppSetting(TimestampMixin, Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class UpdateLog(TimestampMixin, Base):
    __tablename__ = "update_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(80))
    commit_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    previous_commit_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    package_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="received")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback_of_update_id: Mapped[int | None] = mapped_column(ForeignKey("update_logs.id"), nullable=True, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)

class PaymentEvent(TimestampMixin, Base):
    """Append-only payment timeline event used for support and audit."""

    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), default="recorded", server_default="recorded", index=True)
    actor_type: Mapped[str] = mapped_column(String(32), default="system", server_default="system", index=True)
    actor_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class MerchantTeamMember(TimestampMixin, Base):
    """Role-based access entry for a merchant team member."""

    __tablename__ = "merchant_team_members"
    __table_args__ = (UniqueConstraint("merchant_id", "telegram_user_id", name="uq_merchant_team_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(24), default="viewer", server_default="viewer", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
    invited_by_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_access_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SmsDevice(TimestampMixin, Base):
    """Registered SMS Forwarder device with derived HMAC credentials."""

    __tablename__ = "sms_devices"
    __table_args__ = (UniqueConstraint("merchant_id", "device_id", name="uq_sms_device_merchant_device"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(120), default="SMS Forwarder")
    secret_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    allowed_bank_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
    require_hmac: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_signature_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class MerchantOptionProfile(TimestampMixin, Base):
    """Merchant-level preferences and feature controls for commerce options."""

    __tablename__ = "merchant_option_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), unique=True, index=True)
    locale: Mapped[str] = mapped_column(String(12), default="fa", server_default="fa")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Tehran", server_default="Asia/Tehran")
    retention_days: Mapped[int] = mapped_column(Integer, default=180, server_default="180")
    low_balance_threshold_rial: Mapped[int] = mapped_column(BigInteger, default=1_000_000, server_default="1000000")
    notifications_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    feature_flags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    emergency_mode: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    public_status_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    custom_domain: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    anti_phishing_code: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Branch(TimestampMixin, Base):
    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("merchant_id", "code", name="uq_branch_merchant_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(40), index=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    manager_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("merchant_id", "external_id", name="uq_customer_merchant_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(160), default="مشتری")
    phone_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_last4: Mapped[str | None] = mapped_column(String(4), nullable=True, index=True)
    email_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    tags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    portal_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    portal_token_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    total_spend_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    purchase_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_payment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("merchant_id", "slug", name="uq_product_merchant_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_rial: Mapped[int] = mapped_column(BigInteger)
    inventory_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    fulfillment_type: Mapped[str] = mapped_column(String(40), default="manual", server_default="manual", index=True)
    fulfillment_config_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (UniqueConstraint("merchant_id", "code", name="uq_campaign_merchant_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140))
    code: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    medium: Mapped[str | None] = mapped_column(String(80), nullable=True)
    budget_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class PaymentLink(TimestampMixin, Base):
    __tablename__ = "payment_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    fixed_amount_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    min_amount_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_amount_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fee_mode: Mapped[str] = mapped_column(String(20), default="default", server_default="default")
    ttl_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    completion_mode: Mapped[str] = mapped_column(String(20), default="exact", server_default="exact", index=True)
    collect_name: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    collect_phone: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    collect_order_id: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    branding_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class PartialPayment(TimestampMixin, Base):
    __tablename__ = "partial_payments"
    __table_args__ = (UniqueConstraint("invoice_id", "reference_number", name="uq_partial_invoice_reference"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_transactions.id", ondelete="SET NULL"), nullable=True, index=True)
    amount_rial: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual", index=True)
    status: Mapped[str] = mapped_column(String(24), default="accepted", server_default="accepted", index=True)
    reference_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class RefundRequest(TimestampMixin, Base):
    __tablename__ = "refund_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    amount_rial: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="requested", server_default="requested", index=True)
    destination_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AutomationRule(TimestampMixin, Base):
    __tablename__ = "automation_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    trigger: Mapped[str] = mapped_column(String(80), index=True)
    conditions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    actions_json: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
    run_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AutomationExecution(TimestampMixin, Base):
    __tablename__ = "automation_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("automation_rules.id", ondelete="CASCADE"), index=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    trigger: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", server_default="queued", index=True)
    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IntegrationConnector(TimestampMixin, Base):
    __tablename__ = "integration_connectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    connector_type: Mapped[str] = mapped_column(String(48), index=True)
    name: Mapped[str] = mapped_column(String(140))
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class FulfillmentJob(TimestampMixin, Base):
    __tablename__ = "fulfillment_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    connector_id: Mapped[int | None] = mapped_column(ForeignKey("integration_connectors.id", ondelete="SET NULL"), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class MessageTemplate(TimestampMixin, Base):
    __tablename__ = "message_templates"
    __table_args__ = (UniqueConstraint("merchant_id", "store_id", "event_type", "channel", name="uq_message_template_scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    channel: Mapped[str] = mapped_column(String(24), default="telegram", server_default="telegram", index=True)
    name: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class WebhookSubscription(TimestampMixin, Base):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    url: Mapped[str] = mapped_column(Text)
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
    last_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CardRoutingRule(TimestampMixin, Base):
    __tablename__ = "card_routing_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140))
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100", index=True)
    conditions_json: Mapped[str] = mapped_column(Text)
    card_id: Mapped[int] = mapped_column(ForeignKey("bank_cards.id", ondelete="CASCADE"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class SmsParserTemplate(TimestampMixin, Base):
    __tablename__ = "sms_parser_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    bank_code: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(140))
    sender_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    credit_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_pattern: Mapped[str] = mapped_column(Text)
    card_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, default=80, server_default="80")
    tested_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)


class FraudRule(TimestampMixin, Base):
    __tablename__ = "fraud_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140))
    rule_code: Mapped[str] = mapped_column(String(80), index=True)
    conditions_json: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(24), default="review", server_default="review", index=True)
    score: Mapped[int] = mapped_column(Integer, default=25, server_default="25")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class AdminInboxItem(TimestampMixin, Base):
    __tablename__ = "admin_inbox_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id", ondelete="SET NULL"), nullable=True, index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="medium", server_default="medium", index=True)
    status: Mapped[str] = mapped_column(String(24), default="open", server_default="open", index=True)
    title: Mapped[str] = mapped_column(String(180))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MerchantVerification(TimestampMixin, Base):
    __tablename__ = "merchant_verifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), unique=True, index=True)
    level: Mapped[str] = mapped_column(String(32), default="phone", server_default="phone", index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending", index=True)
    business_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    verified_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trust_badge_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class CustomerWallet(TimestampMixin, Base):
    __tablename__ = "customer_wallets"
    __table_args__ = (UniqueConstraint("merchant_id", "customer_id", name="uq_customer_wallet"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    balance_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    points: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    tier: Mapped[str] = mapped_column(String(24), default="bronze", server_default="bronze", index=True)


class CustomerWalletEntry(TimestampMixin, Base):
    __tablename__ = "customer_wallet_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("customer_wallets.id", ondelete="CASCADE"), index=True)
    entry_type: Mapped[str] = mapped_column(String(40), index=True)
    amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    points: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    reference_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)


class CashierShift(TimestampMixin, Base):
    __tablename__ = "cashier_shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"), index=True)
    cashier_telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opening_amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    closing_amount_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="open", index=True)


class AbExperiment(TimestampMixin, Base):
    __tablename__ = "ab_experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140))
    target: Mapped[str] = mapped_column(String(48), default="payment_page", server_default="payment_page", index=True)
    status: Mapped[str] = mapped_column(String(24), default="draft", server_default="draft", index=True)
    allocation_percent: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AbVariant(TimestampMixin, Base):
    __tablename__ = "ab_variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("ab_experiments.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    weight: Mapped[int] = mapped_column(Integer, default=50, server_default="50")
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    views: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    conversions: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class AnalyticsEvent(TimestampMixin, Base):
    __tablename__ = "analytics_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    payment_link_id: Mapped[int | None] = mapped_column(ForeignKey("payment_links.id", ondelete="SET NULL"), nullable=True, index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True)
    experiment_id: Mapped[int | None] = mapped_column(ForeignKey("ab_experiments.id", ondelete="SET NULL"), nullable=True, index=True)
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("ab_variants.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class InvoiceTemplate(TimestampMixin, Base):
    __tablename__ = "invoice_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(140))
    amount_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    fee_mode: Mapped[str] = mapped_column(String(20), default="default", server_default="default")
    ttl_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    card_id: Mapped[int | None] = mapped_column(ForeignKey("bank_cards.id", ondelete="SET NULL"), nullable=True, index=True)
    settings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class SubscriptionPlan(TimestampMixin, Base):
    __tablename__ = "subscription_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    amount_rial: Mapped[int] = mapped_column(BigInteger)
    interval_days: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    grace_days: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    max_cycles: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_create_invoice: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("subscription_plans.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", server_default="active", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    next_invoice_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    current_cycle: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DiscountCode(TimestampMixin, Base):
    __tablename__ = "discount_codes"
    __table_args__ = (UniqueConstraint("merchant_id", "code", name="uq_discount_merchant_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True, index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    discount_type: Mapped[str] = mapped_column(String(20), default="percent", server_default="percent")
    value: Mapped[int] = mapped_column(BigInteger)
    min_amount_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_discount_rial: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    new_customers_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class Affiliate(TimestampMixin, Base):
    __tablename__ = "affiliates"
    __table_args__ = (UniqueConstraint("merchant_id", "code", name="uq_affiliate_merchant_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(140))
    code: Mapped[str] = mapped_column(String(64), index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    commission_type: Mapped[str] = mapped_column(String(20), default="percent", server_default="percent")
    commission_value: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    wallet_balance_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class AffiliateCommission(TimestampMixin, Base):
    __tablename__ = "affiliate_commissions"
    __table_args__ = (UniqueConstraint("affiliate_id", "invoice_id", name="uq_affiliate_invoice_commission"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    affiliate_id: Mapped[int] = mapped_column(ForeignKey("affiliates.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    amount_rial: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending", index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupportTicket(TimestampMixin, Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)
    subject: Mapped[str] = mapped_column(String(180))
    category: Mapped[str] = mapped_column(String(64), default="general", server_default="general", index=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal", server_default="normal", index=True)
    status: Mapped[str] = mapped_column(String(24), default="open", server_default="open", index=True)
    assigned_to: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SupportMessage(TimestampMixin, Base):
    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True)
    sender_type: Mapped[str] = mapped_column(String(24), index=True)
    sender_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    attachment_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class PaymentReminder(TimestampMixin, Base):
    __tablename__ = "payment_reminders"
    __table_args__ = (UniqueConstraint("invoice_id", "scheduled_at", "channel", name="uq_invoice_reminder"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    channel: Mapped[str] = mapped_column(String(24), default="telegram", server_default="telegram")
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PaymentRequest(TimestampMixin, Base):
    __tablename__ = "payment_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    amount_rial: Mapped[int] = mapped_column(BigInteger)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_channel: Mapped[str] = mapped_column(String(24), default="telegram", server_default="telegram")
    delivery_target: Mapped[str | None] = mapped_column(String(180), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="draft", server_default="draft", index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True)


class ScheduledInvoice(TimestampMixin, Base):
    __tablename__ = "scheduled_invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("invoice_templates.id", ondelete="CASCADE"), index=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    interval_days: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    remaining_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)
