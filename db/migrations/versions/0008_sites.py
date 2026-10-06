"""Sites, who looks after them, and which site a printer is in

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("map_x", sa.Integer, nullable=False, server_default="0"),
        sa.Column("map_y", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "user_site",
        sa.Column("username", sa.String(64), sa.ForeignKey("app_user.username"),
                  primary_key=True),
        sa.Column("site_id", sa.String(64), sa.ForeignKey("site.id"), primary_key=True),
    )
    op.create_index("ix_user_site_site_id", "user_site", ["site_id"])
    # batch so SQLite can add a foreign key to a table that already exists
    with op.batch_alter_table("printer") as batch:
        batch.add_column(sa.Column("site_id", sa.String(64)))
        batch.add_column(sa.Column("grid_x", sa.Integer))
        batch.add_column(sa.Column("grid_y", sa.Integer))
        batch.create_foreign_key("fk_printer_site", "site", ["site_id"], ["id"])
        batch.create_index("ix_printer_site_id", ["site_id"])


def downgrade() -> None:
    with op.batch_alter_table("printer") as batch:
        batch.drop_index("ix_printer_site_id")
        batch.drop_constraint("fk_printer_site", type_="foreignkey")
        batch.drop_column("grid_y")
        batch.drop_column("grid_x")
        batch.drop_column("site_id")
    op.drop_index("ix_user_site_site_id", "user_site")
    op.drop_table("user_site")
    op.drop_table("site")
