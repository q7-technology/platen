"""Page templates, page printers, and PDF documents on a run

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("template", sa.Column("kind", sa.String(8), nullable=False,
                                        server_default="label"))
    op.add_column("template", sa.Column("page", sa.JSON))
    op.add_column("printer", sa.Column("kind", sa.String(8), nullable=False,
                                       server_default="label"))
    op.add_column("run_label", sa.Column("body", sa.LargeBinary))


def downgrade() -> None:
    with op.batch_alter_table("run_label") as batch:
        batch.drop_column("body")
    with op.batch_alter_table("printer") as batch:
        batch.drop_column("kind")
    with op.batch_alter_table("template") as batch:
        batch.drop_column("page")
        batch.drop_column("kind")
