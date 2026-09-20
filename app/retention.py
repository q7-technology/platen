"""Keeping the database from growing forever.

A five thousand label run writes about twenty megabytes of ZPL into
`run_label`. Nobody ever reads last April's label data, but everybody wants to
know what happened on the day, so the labels go first and the run's history —
what printed, what warned, what went wrong — stays behind.

Nothing that has not finished is ever touched, however old it looks. A queued
job from last year is still a job somebody might print.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from db.models import AgentJob, AuditLog, PrintRun, RunLabel, RunWarning, Setting, UserSession

log = logging.getLogger("platen.retention")

SETTING = "retention"
LAST_RUN = "maintenance.last_pruned"

# Days. Zero means keep it, which is a choice somebody has to make on purpose.
DEFAULTS: dict[str, int] = {
    "labels_days": 14,      # the ZPL of a finished run
    "runs_days": 365,       # the run itself, its warnings and its history
    "audit_days": 365,
    "agent_jobs_days": 7,   # labels an agent has already printed
}

# A run in one of these is finished; anything else might still print.
SETTLED = ("done", "cancelled")


def now() -> datetime:
    return datetime.now(UTC)


def settings(session: Session) -> dict[str, int]:
    row = session.get(Setting, SETTING)
    stored = row.value if row else {}
    return {k: int(stored.get(k, v)) for k, v in DEFAULTS.items()}


def set_settings(session: Session, changes: dict[str, Any]) -> dict[str, int]:
    current = settings(session)
    for key, value in changes.items():
        if key in DEFAULTS and value is not None:
            current[key] = max(0, int(value))
    row = session.get(Setting, SETTING) or Setting(key=SETTING, value={})
    row.value = current
    session.add(row)
    session.commit()
    return current


def _cutoff(days: int) -> datetime | None:
    return None if days <= 0 else now() - timedelta(days=days)


def prune(session: Session) -> dict[str, int]:
    """Delete what has aged out. Safe to run as often as you like."""
    keep = settings(session)
    removed = {"labels": 0, "runs": 0, "warnings": 0, "audit": 0,
               "sessions": 0, "agent_jobs": 0}

    finished = func.coalesce(PrintRun.finished_at, PrintRun.created_at)

    # 1. the ZPL of settled runs, which is the bulk of it
    if (cutoff := _cutoff(keep["labels_days"])) is not None:
        ids = list(session.scalars(
            select(PrintRun.id).where(PrintRun.status.in_(SETTLED), finished < cutoff)))
        if ids:
            removed["labels"] = session.execute(
                delete(RunLabel).where(RunLabel.run_id.in_(ids))).rowcount or 0

    # 2. whole runs, once even their history has aged out
    if (cutoff := _cutoff(keep["runs_days"])) is not None:
        ids = list(session.scalars(
            select(PrintRun.id).where(PrintRun.status.in_(SETTLED), finished < cutoff)))
        if ids:
            removed["labels"] += session.execute(
                delete(RunLabel).where(RunLabel.run_id.in_(ids))).rowcount or 0
            removed["warnings"] = session.execute(
                delete(RunWarning).where(RunWarning.run_id.in_(ids))).rowcount or 0
            removed["runs"] = session.execute(
                delete(PrintRun).where(PrintRun.id.in_(ids))).rowcount or 0

    if (cutoff := _cutoff(keep["audit_days"])) is not None:
        removed["audit"] = session.execute(
            delete(AuditLog).where(AuditLog.at < cutoff)).rowcount or 0

    # a label an agent never collected is not rubbish, so only settled ones go
    if (cutoff := _cutoff(keep["agent_jobs_days"])) is not None:
        removed["agent_jobs"] = session.execute(
            delete(AgentJob).where(AgentJob.done_at.isnot(None),
                                   AgentJob.done_at < cutoff)).rowcount or 0

    # a session nobody came back for is only noticed when somebody tries to
    # use it, so they would otherwise sit there for good
    removed["sessions"] = session.execute(
        delete(UserSession).where(UserSession.expires_at < now())).rowcount or 0

    session.commit()
    if any(removed.values()):
        log.info("pruned %s", ", ".join(f"{n} {k}" for k, n in removed.items() if n))
    return removed


def would_remove(session: Session) -> dict[str, int]:
    """What a prune would take, without taking it."""
    keep = settings(session)
    finished = func.coalesce(PrintRun.finished_at, PrintRun.created_at)
    counts = dict.fromkeys(("labels", "runs", "warnings", "audit", "sessions",
                            "agent_jobs"), 0)

    def count(stmt) -> int:
        return session.scalar(stmt) or 0

    if (cutoff := _cutoff(keep["labels_days"])) is not None:
        counts["labels"] = count(
            select(func.count()).select_from(RunLabel).join(PrintRun)
            .where(PrintRun.status.in_(SETTLED), finished < cutoff))
    if (cutoff := _cutoff(keep["runs_days"])) is not None:
        counts["runs"] = count(
            select(func.count()).select_from(PrintRun)
            .where(PrintRun.status.in_(SETTLED), finished < cutoff))
    if (cutoff := _cutoff(keep["audit_days"])) is not None:
        counts["audit"] = count(
            select(func.count()).select_from(AuditLog).where(AuditLog.at < cutoff))
    if (cutoff := _cutoff(keep["agent_jobs_days"])) is not None:
        counts["agent_jobs"] = count(
            select(func.count()).select_from(AgentJob)
            .where(AgentJob.done_at.isnot(None), AgentJob.done_at < cutoff))
    counts["sessions"] = count(
        select(func.count()).select_from(UserSession)
        .where(UserSession.expires_at < now()))
    return counts


def due(session: Session, every: timedelta = timedelta(days=1)) -> bool:
    """True once per window, and claims it, so several API processes sharing a
    database do not all prune at the same moment."""
    row = session.get(Setting, LAST_RUN)
    last = row.value.get("at") if row else None
    if last and datetime.fromisoformat(last) > now() - every:
        return False
    row = row or Setting(key=LAST_RUN, value={})
    row.value = {"at": now().isoformat()}
    session.add(row)
    session.commit()
    return True
