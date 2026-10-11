"""Report a print job back to whoever sent it: a callback on each key

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_token", sa.Column("callback_url", sa.Text))
    op.add_column("api_token", sa.Column("callback_key", sa.Text))
    op.add_column("print_run", sa.Column("sent_by_key", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("print_run") as batch:
        batch.drop_column("sent_by_key")
    with op.batch_alter_table("api_token") as batch:
        batch.drop_column("callback_key")
        batch.drop_column("callback_url")
