"""Moving a run to another printer, and detouring a printer's work

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("print_run", sa.Column("move_to", sa.String(64)))
    op.add_column("printer", sa.Column("detour_to", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("printer") as batch:
        batch.drop_column("detour_to")
    with op.batch_alter_table("print_run") as batch:
        batch.drop_column("move_to")
