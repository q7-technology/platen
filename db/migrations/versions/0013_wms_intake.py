"""Print jobs from Simple WMS

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("print_run", sa.Column("wms_job_id", sa.String(36)))
    op.create_index("ix_print_run_wms_job_id", "print_run", ["wms_job_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_print_run_wms_job_id", table_name="print_run")
    with op.batch_alter_table("print_run") as batch:
        batch.drop_column("wms_job_id")
