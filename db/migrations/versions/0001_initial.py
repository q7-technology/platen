"""The ten tables

Revision ID: 0001
Revises:
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "template",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("width_mm", sa.Float, nullable=False),
        sa.Column("height_mm", sa.Float, nullable=False),
        sa.Column("dpi", sa.Integer, nullable=False),
        sa.Column("darkness", sa.Integer),
        sa.Column("datasource_id", sa.String(64)),
        sa.Column("query_id", sa.String(64)),
        sa.Column("elements", sa.JSON, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
    )
    op.create_table(
        "template_version",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("template_id", sa.String(64), sa.ForeignKey("template.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("definition", sa.JSON, nullable=False),
        sa.Column("published_at", TZ, nullable=False),
        sa.UniqueConstraint("template_id", "version"),
    )
    op.create_table(
        "datasource",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("pool_size", sa.Integer, nullable=False),
    )
    op.create_table(
        "saved_query",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("datasource_id", sa.String(64), sa.ForeignKey("datasource.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sql", sa.Text, nullable=False),
    )
    op.create_table(
        "query_parameter",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("query_id", sa.String(64), sa.ForeignKey("saved_query.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("default", sa.Text),
        sa.Column("ask_at_print", sa.Boolean, nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.UniqueConstraint("query_id", "name"),
    )
    op.create_table(
        "printer",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("dpi", sa.Integer, nullable=False),
        sa.Column("transport_kind", sa.String(16), nullable=False),
        sa.Column("transport_config", sa.JSON, nullable=False),
    )
    op.create_table(
        "print_run",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("template_version_id", sa.Integer, sa.ForeignKey("template_version.id"), nullable=False),
        sa.Column("printer_id", sa.String(64), sa.ForeignKey("printer.id"), nullable=False),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("copies", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("printed", sa.Integer, nullable=False),
        sa.Column("total", sa.Integer, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("cancel_requested", sa.Boolean, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("started_at", TZ),
        sa.Column("finished_at", TZ),
    )
    op.create_index("ix_print_run_created_at", "print_run", ["created_at"])
    op.create_table(
        "run_label",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("print_run.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("zpl", sa.Text, nullable=False),
        sa.Column("printed_at", TZ),
        sa.UniqueConstraint("run_id", "seq"),
    )
    op.create_table(
        "run_warning",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("print_run.id"), nullable=False),
        sa.Column("row_no", sa.Integer),
        sa.Column("message", sa.Text, nullable=False),
    )
    op.create_index("ix_run_warning_run_id", "run_warning", ["run_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("at", TZ, nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("detail", sa.JSON, nullable=False),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])


def downgrade() -> None:
    for name in ("audit_log", "run_warning", "run_label", "print_run", "printer",
                 "query_parameter", "saved_query", "datasource", "template_version", "template"):
        op.drop_table(name)
