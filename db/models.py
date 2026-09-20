"""The ten tables. Types are kept portable so the same models run on Postgres
in production and on SQLite in the test suite."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """A timestamp that always comes back knowing it is UTC.

    Postgres keeps the offset; SQLite has no time zones and hands back a naive
    value, which a browser then reads as local time — a label edited a minute
    ago claims to be ten hours old in Ballarat. Both ends are pinned here so
    every reader gets the same answer.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class AppUser(Base):
    """Named app_user because `user` is reserved in Postgres."""

    __tablename__ = "app_user"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(16), default="operator")
    password: Mapped[str] = mapped_column(Text)        # scrypt$n$r$p$salt$hash
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class UserSession(Base):
    """One browser, signed in. The id is a hash of the token, so the database
    never holds anything that could be replayed as a cookie."""

    __tablename__ = "user_session"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(ForeignKey("app_user.username"), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now)


class Setting(Base):
    """A handful of things that are true of the whole instance, like whether
    the queue is paused. Kept in the database so the API and the worker see
    the same answer."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    changed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now, onupdate=now)


class ApiToken(Base):
    """A key something other than a person signs in with. Only its hash is
    kept, so the plain token exists once, in the answer that created it."""

    __tablename__ = "api_token"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16))          # admin | operator | agent
    agent_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class PrintAgent(Base):
    """A workstation with a printer hanging off it. It asks Platen for work
    rather than Platen reaching into the office network."""

    __tablename__ = "print_agent"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    devices: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class AgentJob(Base):
    """One label waiting for an agent to come and get it."""

    __tablename__ = "agent_job"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("print_agent.id"), index=True)
    device: Mapped[str] = mapped_column(String(200), default="")
    zpl: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now)
    taken_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    done_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class Template(Base):
    """The draft the editor works on. Publishing snapshots it into a version."""

    __tablename__ = "template"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    width_mm: Mapped[float]
    height_mm: Mapped[float]
    dpi: Mapped[int] = mapped_column(Integer, default=203)
    darkness: Mapped[int | None]
    folder: Mapped[str] = mapped_column(String(100), default="")
    datasource_id: Mapped[str | None] = mapped_column(String(64))
    query_id: Mapped[str | None] = mapped_column(String(64))
    elements: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now, onupdate=now)

    versions: Mapped[list[TemplateVersion]] = relationship(
        back_populates="template", order_by="TemplateVersion.version",
        cascade="all, delete-orphan",
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
    published_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now)

    template: Mapped[Template] = relationship(back_populates="versions")


class DataSource(Base):
    __tablename__ = "datasource"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    label: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(8), default="sql")     # sql | rest
    url: Mapped[str] = mapped_column(Text)
    headers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)   # rest only
    pool_size: Mapped[int] = mapped_column(Integer, default=5)


class SavedQuery(Base):
    __tablename__ = "saved_query"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    datasource_id: Mapped[str] = mapped_column(ForeignKey("datasource.id"))
    name: Mapped[str] = mapped_column(String(200))
    sql: Mapped[str] = mapped_column(Text)          # or a request path, for rest
    row_path: Mapped[str] = mapped_column(String(200), default="")

    parameters: Mapped[list[QueryParameter]] = relationship(
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
    pause_between: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    printed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    template_version: Mapped[TemplateVersion] = relationship()
    labels: Mapped[list[RunLabel]] = relationship(
        back_populates="run", order_by="RunLabel.seq", cascade="all, delete-orphan"
    )
    warnings: Mapped[list[RunWarning]] = relationship(
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
    printed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    run: Mapped[PrintRun] = relationship(back_populates="labels")


class RunWarning(Base):
    __tablename__ = "run_warning"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("print_run.id"), index=True)
    row_no: Mapped[int | None]
    message: Mapped[str] = mapped_column(Text)

    run: Mapped[PrintRun] = relationship(back_populates="warnings")


class SavedRun(Base):
    """A run somebody does every morning, kept so they do not set it up again."""

    __tablename__ = "saved_run"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    template_id: Mapped[str] = mapped_column(ForeignKey("template.id"))
    printer_id: Mapped[str | None] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    copies: Mapped[int] = mapped_column(Integer, default=1)
    separator: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_between: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=now, onupdate=now)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, default=now, index=True)
    actor: Mapped[str] = mapped_column(String(200), default="")
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
