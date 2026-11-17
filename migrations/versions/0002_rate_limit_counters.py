"""Shared rate-limit counters.

Revision ID: 0002_rate_limit_counters
Revises: 0001_initial_schema
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_rate_limit_counters"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("window_start", sa.Integer, primary_key=True),
        sa.Column("count", sa.Integer, nullable=False),
    )


def downgrade():
    op.drop_table("rate_limit_counters")
