"""Folders, saved runs, and printing one at a time

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("template",
                  sa.Column("folder", sa.String(100), nullable=False, server_default=""))
    op.add_column("print_run",
                  sa.Column("pause_between", sa.Boolean, nullable=False,
                            server_default=sa.false()))
    op.create_table(
        "saved_run",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("template_id", sa.String(64), sa.ForeignKey("template.id"), nullable=False),
        sa.Column("printer_id", sa.String(64)),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("copies", sa.Integer, nullable=False),
        sa.Column("separator", sa.Boolean, nullable=False),
        sa.Column("pause_between", sa.Boolean, nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("saved_run")
    op.drop_column("print_run", "pause_between")
    op.drop_column("template", "folder")
