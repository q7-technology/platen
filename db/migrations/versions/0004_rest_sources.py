"""Data sources that are an endpoint rather than a database

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("datasource",
                  sa.Column("kind", sa.String(8), nullable=False, server_default="sql"))
    op.add_column("datasource",
                  sa.Column("headers", sa.JSON, nullable=False, server_default="{}"))
    op.add_column("saved_query",
                  sa.Column("row_path", sa.String(200), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("saved_query", "row_path")
    op.drop_column("datasource", "headers")
    op.drop_column("datasource", "kind")
