"""BluePay commerce options, automation and customer experience.

Revision ID: 20260801_1200
Revises: 20260801_1100
Create Date: 2026-08-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.db.base import Base
import app.models  # noqa: F401

revision = "20260801_1200"
down_revision = "20260801_1100"
branch_labels = None
depends_on = None


def _add_columns_if_missing(bind, table_name: str, columns: list[sa.Column]) -> None:
    inspector = inspect(bind)
    if table_name not in set(inspector.get_table_names()):
        return
    existing = {item["name"] for item in inspector.get_columns(table_name)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)
    _add_columns_if_missing(bind, "invoices", [
        sa.Column("customer_id", sa.BigInteger(), nullable=True),
        sa.Column("payment_link_id", sa.BigInteger(), nullable=True),
        sa.Column("campaign_id", sa.BigInteger(), nullable=True),
        sa.Column("branch_id", sa.BigInteger(), nullable=True),
        sa.Column("received_amount_rial", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("completion_mode", sa.String(20), nullable=False, server_default="exact"),
        sa.Column("source_channel", sa.String(40), nullable=True),
        sa.Column("ab_variant_id", sa.BigInteger(), nullable=True),
        sa.Column("discount_id", sa.BigInteger(), nullable=True),
        sa.Column("affiliate_id", sa.BigInteger(), nullable=True),
        sa.Column("subscription_id", sa.BigInteger(), nullable=True),
    ])
    # Base.metadata.create_all() may already have created these indexes on a
    # fresh PostgreSQL database.  Do not rely on catching duplicate-index
    # errors: PostgreSQL marks the entire migration transaction as aborted
    # after such an error, even when Python catches the exception.
    existing_indexes = {
        item.get("name")
        for item in inspect(bind).get_indexes("invoices")
        if item.get("name")
    }
    for name, column in [
        ("ix_invoices_customer_id", "customer_id"),
        ("ix_invoices_payment_link_id", "payment_link_id"),
        ("ix_invoices_campaign_id", "campaign_id"),
        ("ix_invoices_branch_id", "branch_id"),
        ("ix_invoices_completion_mode", "completion_mode"),
        ("ix_invoices_source_channel", "source_channel"),
        ("ix_invoices_ab_variant_id", "ab_variant_id"),
        ("ix_invoices_discount_id", "discount_id"),
        ("ix_invoices_affiliate_id", "affiliate_id"),
        ("ix_invoices_subscription_id", "subscription_id"),
    ]:
        if name not in existing_indexes:
            op.create_index(name, "invoices", [column], unique=False)
            existing_indexes.add(name)


def downgrade() -> None:
    # Commerce and financial history is intentionally retained on rollback.
    pass
