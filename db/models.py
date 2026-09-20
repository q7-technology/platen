"""The ten tables. Types are kept portable so the same models run on Postgres
in production and on SQLite in the test suite."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Template(Base):
    """The draft the editor works on. Publishing snapshots it into a version."""

    __tablename__ = "template"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    width_mm: Mapped[float]
    height_mm: Mapped[float]
    dpi: Mapped[int] = mapped_column(Integer, default=203)
    darkness: Mapped[int | None]
    datasource_id: Mapped[str | None] = mapped_column(String(64))
    query_id: Mapped[str | None] = mapped_column(String(64))
    elements: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )

    versions: Mapped[list["TemplateVersion"]] = relationship(
        back_populates="template", order_by="TemplateVersion.version"
    )


class TemplateVersion(Base):
    """Immutable. A print run points at one of these, so what was printed can
    always be reproduced even after the draft has moved on."""

    __tablename__ = "template_version"
    __table_args__ = (UniqueConstraint("template_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("template.id"))
    version: Mapped[int]
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    template: Mapped[Template] = relationship(back_populates="versions")


class DataSource(Base):
    __tablename__ = "datasource"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    label: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(Text)
    pool_size: Mapped[int] = mapped_column(Integer, default=5)


class SavedQuery(Base):
    __tablename__ = "saved_query"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    datasource_id: Mapped[str] = mapped_column(ForeignKey("datasource.id"))
    name: Mapped[str] = mapped_column(String(200))
    sql: Mapped[str] = mapped_column(Text)

    parameters: Mapped[list["QueryParameter"]] = relationship(
        back_populates="query", order_by="QueryParameter.position",
        cascade="all, delete-orphan",
    )


class QueryParameter(Base):
    __tablename__ = "query_parameter"
    __table_args__ = (UniqueConstraint("query_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    query_id: Mapped[str] = mapped_column(ForeignKey("saved_query.id"))
    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(16), default="text")
    default: Mapped[str | None] = mapped_column(Text)
    ask_at_print: Mapped[bool] = mapped_column(Boolean, default=True)
    label: Mapped[str] = mapped_column(String(200), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)

    query: Mapped[SavedQuery] = relationship(back_populates="parameters")


class Printer(Base):
    __tablename__ = "printer"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    model: Mapped[str] = mapped_column(String(64), default="")
    dpi: Mapped[int] = mapped_column(Integer, default=203)
    transport_kind: Mapped[str] = mapped_column(String(16))
    transport_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PrintRun(Base):
    __tablename__ = "print_run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    template_version_id: Mapped[int] = mapped_column(ForeignKey("template_version.id"))
    printer_id: Mapped[str] = mapped_column(ForeignKey("printer.id"))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    copies: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    printed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    template_version: Mapped[TemplateVersion] = relationship()
    labels: Mapped[list["RunLabel"]] = relationship(
        back_populates="run", order_by="RunLabel.seq", cascade="all, delete-orphan"
    )
    warnings: Mapped[list["RunWarning"]] = relationship(
        back_populates="run", order_by="RunWarning.id", cascade="all, delete-orphan"
    )


class RunLabel(Base):
    """One physical label's worth of ZPL, rendered before the run was queued."""

    __tablename__ = "run_label"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("print_run.id"))
    seq: Mapped[int]
    zpl: Mapped[str] = mapped_column(Text)
    printed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[PrintRun] = relationship(back_populates="labels")


class RunWarning(Base):
    __tablename__ = "run_warning"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("print_run.id"), index=True)
    row_no: Mapped[int | None]
    message: Mapped[str] = mapped_column(Text)

    run: Mapped[PrintRun] = relationship(back_populates="warnings")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    actor: Mapped[str] = mapped_column(String(200), default="")
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
