"""People, and their sessions

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("username", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("password", sa.Text, nullable=False),
        sa.Column("disabled", sa.Boolean, nullable=False),
        sa.Column("failed_logins", sa.Integer, nullable=False),
        sa.Column("locked_until", TZ),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("last_login_at", TZ),
    )
    op.create_table(
        "user_session",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(64), sa.ForeignKey("app_user.username"), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("last_seen_at", TZ, nullable=False),
    )
    op.create_index("ix_user_session_username", "user_session", ["username"])


def downgrade() -> None:
    op.drop_table("user_session")
    op.drop_table("app_user")
