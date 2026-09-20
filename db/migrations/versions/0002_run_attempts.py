"""Count a run's attempts

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("print_run",
                  sa.Column("attempts", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("print_run", "attempts")
