"""Machine tokens, agents and the labels waiting for them

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "api_token",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("agent_id", sa.String(64)),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("last_used_at", TZ),
    )
    op.create_index("ix_api_token_agent_id", "api_token", ["agent_id"])
    op.create_table(
        "print_agent",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("devices", sa.JSON, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("last_seen_at", TZ),
    )
    op.create_table(
        "agent_job",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("agent_id", sa.String(64), sa.ForeignKey("print_agent.id"), nullable=False),
        sa.Column("device", sa.String(200), nullable=False),
        sa.Column("zpl", sa.Text, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("taken_at", TZ),
        sa.Column("done_at", TZ),
    )
    op.create_index("ix_agent_job_agent_id", "agent_job", ["agent_id"])


def downgrade() -> None:
    op.drop_table("agent_job")
    op.drop_table("print_agent")
    op.drop_table("api_token")
