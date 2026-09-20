"""The queue. By the time a job gets here every label is already rendered and
sitting in run_label, so the worker does nothing that can fail for an
interesting reason. Redis carries only the job id; the run itself is a row."""

from __future__ import annotations

from datetime import datetime, timezone

import redis
from rq import Queue
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import session as dbsession
from db.models import PrintRun, RunLabel

from . import printers

_redis: redis.Redis | None = None


def bind(conn: redis.Redis) -> None:
    global _redis
    _redis = conn


def connection() -> redis.Redis:
    if _redis is None:
        from .settings import settings

        bind(redis.Redis.from_url(settings.redis_url))
    assert _redis is not None
    return _redis


def queue() -> Queue:
    return Queue("platen", connection=connection())


def enqueue(run_id: str) -> None:
    """Call only after the run and all its labels are committed."""
    queue().enqueue(print_run, run_id, job_timeout=3600, retry=None)


def cancel(session: Session, run_id: str) -> PrintRun:
    run = session.get(PrintRun, run_id)
    if run is None:
        raise KeyError(run_id)
    run.cancel_requested = True
    session.commit()
    return run


def _cancelled(session: Session, run_id: str) -> bool:
    # commit first so progress lands and the next read starts a fresh
    # transaction: the API sets this flag from another process
    session.commit()
    return bool(session.scalar(select(PrintRun.cancel_requested).where(PrintRun.id == run_id)))


def print_run(run_id: str) -> None:
    """Runs in the worker process."""
    dbsession.engine()
    with dbsession.SessionLocal() as s:
        run = s.get(PrintRun, run_id)
        if run is None:
            raise KeyError(run_id)
        printer = printers.load(s, run.printer_id)
        if printer is None:
            run.status, run.error = "failed", f"printer {run.printer_id!r} no longer exists"
            run.finished_at = datetime.now(timezone.utc)
            s.commit()
            return

        run.status, run.started_at = "printing", datetime.now(timezone.utc)
        s.commit()
        labels = s.scalars(
            select(RunLabel).where(RunLabel.run_id == run_id).order_by(RunLabel.seq)
        ).all()

        try:
            for label in labels:
                if _cancelled(s, run_id):
                    run.status = "cancelled"
                    return
                # one label per write: the printer buffers a few, and a mid-run
                # failure then costs one label instead of the whole batch
                printer.transport.send(label.zpl.encode("ascii") + b"\n")
                label.printed_at = datetime.now(timezone.utc)
                run.printed = label.seq
            run.status = "done"
        except Exception as exc:                      # noqa: BLE001 — recorded, not swallowed
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            run.finished_at = datetime.now(timezone.utc)
            s.commit()
