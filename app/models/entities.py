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
    callback_secret: Mapped[str | None] = mapped_column(String(160), nullable=True)

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
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="RESTRICT"), index=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("bank_cards.id", ondelete="RESTRICT"), index=True)
    order_id: Mapped[str] = mapped_column(String(120), index=True)
    client_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    base_amount_rial: Mapped[int] = mapped_column(BigInteger)
    fee_amount_rial: Mapped[int] = mapped_column(BigInteger)
    customer_fee_rial: Mapped[int] = mapped_column(BigInteger)
    unique_amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    payable_amount_rial: Mapped[int] = mapped_column(BigInteger, index=True)
    fee_mode: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(30), default="payment", server_default="payment", index=True)
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
    matched_sms_id: Mapped[int | None] = mapped_column(ForeignKey("sms_transactions.id"), nullable=True)
    reference_number: Mapped[str | None] = mapped_column(String(120), nullable=True)

    merchant: Mapped[Merchant] = relationship(back_populates="invoices", foreign_keys=[merchant_id])
    card: Mapped[BankCard] = relationship(back_populates="invoices")
    store: Mapped[Store | None] = relationship(back_populates="invoices", foreign_keys=[store_id])


class AmountReservation(TimestampMixin, Base):
    __tablename__ = "amount_reservations"
    __table_args__ = (
        UniqueConstraint("card_id", "payable_amount_rial", name="uq_active_card_payable_amount"),
    )

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
    balance_after_rial: Mapped[int] = mapped_column(BigInteger)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)


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
