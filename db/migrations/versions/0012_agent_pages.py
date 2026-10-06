"""Pages through a desk agent, and keys that keep to their sites

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_job", sa.Column("body", sa.LargeBinary))
    op.add_column("print_agent", sa.Column("accepts", sa.JSON, nullable=False,
                                           server_default="[]"))
    op.add_column("api_token", sa.Column("sites", sa.JSON, nullable=False,
                                         server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("api_token") as batch:
        batch.drop_column("sites")
    with op.batch_alter_table("print_agent") as batch:
        batch.drop_column("accepts")
    with op.batch_alter_table("agent_job") as batch:
        batch.drop_column("body")
