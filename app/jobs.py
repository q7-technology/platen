"""The queue. By the time a job gets here every label is already rendered and
sitting in run_label, so the worker does nothing that can fail for an
interesting reason. Redis carries only the job id; the run itself is a row."""

from __future__ import annotations

from datetime import UTC, datetime

import redis
from rq import Queue, Retry
from rq.job import get_current_job
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db import session as dbsession
from db.models import PrintRun, RunLabel, Setting

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


# A printer that stopped answering usually starts again: someone closes the
# head, reloads the roll, the switch comes back. Two tries, a minute apart,
# covers most of that without a person. The run resumes, never restarts.
RETRY = Retry(max=2, interval=[20, 60])


def enqueue(run_id: str) -> None:
    """Call only after the run and all its labels are committed."""
    queue().enqueue(print_run, run_id, job_timeout=3600, retry=RETRY)


def remaining(session: Session, run_id: str) -> int:
    """Labels that have not come out of the printer yet."""
    return session.scalar(
        select(func.count()).select_from(RunLabel)
        .where(RunLabel.run_id == run_id, RunLabel.printed_at.is_(None))
    ) or 0


def cancel(session: Session, run_id: str) -> PrintRun:
    run = session.get(PrintRun, run_id)
    if run is None:
        raise KeyError(run_id)
    run.cancel_requested = True
    session.commit()
    return run


PAUSED = "queue.paused"


def paused(session: Session) -> bool:
    """Commit first, like the cancel check: the flag is set from the API, in
    another process, and a stale read would keep printing."""
    session.commit()
    value = session.scalar(select(Setting.value).where(Setting.key == PAUSED))
    return bool(value and value.get("on"))


def set_paused(session: Session, on: bool) -> None:
    row = session.get(Setting, PAUSED) or Setting(key=PAUSED, value={})
    row.value = {"on": on}
    session.add(row)
    session.commit()


def _cancelled(session: Session, run_id: str) -> bool:
    # commit first so progress lands and the next read starts a fresh
    # transaction: the API sets this flag from another process
    session.commit()
    return bool(session.scalar(select(PrintRun.cancel_requested).where(PrintRun.id == run_id)))


def print_run(run_id: str) -> None:
    """Runs in the worker process.

    Only labels that have not printed are sent, so a second attempt picks up
    where the first stopped. Reprinting a consignment barcode that already
    went on a carton is worse than not printing it at all.
    """
    dbsession.engine()
    job = get_current_job()
    with dbsession.SessionLocal() as s:
        run = s.get(PrintRun, run_id)
        if run is None:
            raise KeyError(run_id)
        printer = printers.load(s, run.printer_id)
        if printer is None:
            run.status = "failed"
            run.error = f"printer {run.printer_id!r} no longer exists"
            run.finished_at = datetime.now(UTC)
            s.commit()
            return

        # nothing new starts while the queue is held; resuming puts it back
        if paused(s):
            run.status = "paused"
            s.commit()
            return

        run.status, run.started_at, run.error = "printing", datetime.now(UTC), None
        s.commit()
        printed = run.total - remaining(s, run_id)
        labels = s.scalars(
            select(RunLabel).where(RunLabel.run_id == run_id, RunLabel.printed_at.is_(None))
            .order_by(RunLabel.seq)
        ).all()

        try:
            for label in labels:
                if _cancelled(s, run_id):
                    run.status = "cancelled"
                    run.finished_at = datetime.now(UTC)
                    s.commit()
                    return
                if paused(s):
                    # between labels, like cancel: holding halfway through one
                    # would leave it under the print head
                    run.status = "paused"
                    run.printed = printed
                    s.commit()
                    return
                # one label per write: the printer buffers a few, and a mid-run
                # failure then costs one label instead of the whole batch
                printer.transport.send(label.zpl.encode("ascii") + b"\n")
                label.printed_at = datetime.now(UTC)
                printed += 1
                run.printed = printed
                # hand-fed stock: one label, then wait to be told to carry on.
                # A different kind of stopped from a held queue, so it says so.
                if run.pause_between and printed < run.total:
                    run.status = "waiting"
                    s.commit()
                    return
            run.status = "done"
        except Exception as exc:
            run.attempts += 1
            run.printed = printed
            run.error = f"{type(exc).__name__}: {exc}"
            left = getattr(job, "retries_left", 0) or 0
            run.status = "retrying" if left else "failed"
            run.finished_at = None if left else datetime.now(UTC)
            s.commit()
            raise                                     # rq schedules the next attempt
        run.finished_at = datetime.now(UTC)
        s.commit()
