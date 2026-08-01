"""BluePay stability, team access, timeline and SMS device security.

Revision ID: 20260801_1100
Revises: 20260801_1000
Create Date: 2026-08-01
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.db.base import Base
import app.models  # noqa: F401

revision = "20260801_1100"
down_revision = "20260801_1000"
branch_labels = None
depends_on = None


def _add_columns_if_missing(bind, table_name: str, columns: list[sa.Column]) -> None:
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if table_name not in existing_tables:
        return
    existing = {item["name"] for item in inspector.get_columns(table_name)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def upgrade() -> None:
    bind = op.get_bind()
    # The metadata runner safely creates only missing tables and handles
    # dependency cycles correctly on PostgreSQL.
    Base.metadata.create_all(bind=bind, checkfirst=True)

    _add_columns_if_missing(bind, "merchants", [
        sa.Column("return_url", sa.Text(), nullable=True),
    ])
    _add_columns_if_missing(bind, "update_logs", [
        sa.Column("previous_commit_sha", sa.String(80), nullable=True),
        sa.Column("package_sha256", sa.String(64), nullable=True),
        sa.Column("validation_json", sa.Text(), nullable=True),
        sa.Column("rollback_of_update_id", sa.Integer(), nullable=True),
    ])

    # Keep financial/audit schema forward compatible. No destructive changes.


def downgrade() -> None:
    # Deploying an older app is the rollback mechanism. Audit, timeline, team,
    # and device records are intentionally retained.
    pass
