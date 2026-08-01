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

    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    status: Mapped[str] = mapped_column(String(30), default="received")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
