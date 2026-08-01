"""BluePay enterprise core: outbox, sandbox, audit, risk and reconciliation.

Revision ID: 20260801_1000
Revises:
Create Date: 2026-08-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.db.base import Base
import app.models  # noqa: F401

revision = "20260801_1000"
down_revision = None
branch_labels = None
depends_on = None


def _add_columns_if_missing(bind, table_name: str, columns: list[sa.Column]) -> None:
    inspector = inspect(bind)
    existing = {item["name"] for item in inspector.get_columns(table_name)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    # Create all new tables in foreign-key order without touching existing rows.
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            table.create(bind=bind, checkfirst=True)

    _add_columns_if_missing(bind, "stores", [
        sa.Column("allowed_ips", sa.Text(), nullable=True),
        sa.Column("invoice_rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column("daily_amount_limit_rial", sa.BigInteger(), nullable=True),
    ])
    _add_columns_if_missing(bind, "store_api_keys", [
        sa.Column("last_used_ip", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    ])
    _add_columns_if_missing(bind, "invoices", [
        sa.Column("idempotency_key", sa.String(180), nullable=True),
        sa.Column("environment", sa.String(16), nullable=False, server_default="live"),
        sa.Column("risk_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("risk_status", sa.String(24), nullable=False, server_default="approved"),
        sa.Column("return_url", sa.Text(), nullable=True),
    ])
    _add_columns_if_missing(bind, "wallet_ledger", [
        sa.Column("balance_before_rial", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_before_rial", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_after_rial", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reference_type", sa.String(40), nullable=True),
        sa.Column("reference_id", sa.String(120), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("reversed_entry_id", sa.BigInteger(), nullable=True),
    ])


def downgrade() -> None:
    # Financial and audit data is intentionally never dropped automatically.
    # Rollback is performed by deploying the previous application version while
    # retaining the forward-compatible schema.
    pass
