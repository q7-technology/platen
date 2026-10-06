"""How each printer was, the last time it was asked

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("printer", sa.Column("health", sa.String(16)))
    op.add_column("printer", sa.Column("health_problems", sa.JSON, nullable=False,
                                       server_default="[]"))
    op.add_column("printer", sa.Column("health_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("printer") as batch:
        batch.drop_column("health_at")
        batch.drop_column("health_problems")
        batch.drop_column("health")
