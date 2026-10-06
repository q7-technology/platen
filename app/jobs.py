"""The queue. By the time a job gets here every label is already rendered and
sitting in run_label, so the worker does nothing that can fail for an
interesting reason. Redis carries only the job id; the run itself is a row."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import redis
from rq import Queue, Retry
from rq.job import get_current_job
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from db import session as dbsession
from db.models import Printer as PrinterRow
from db.models import PrintRun, RunLabel, Setting

from . import events, printers
from .logs import where

log = logging.getLogger("platen.jobs")

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


STARTABLE = ("queued", "retrying")

# A health answer older than this is no longer evidence of anything: the
# checker may have stopped, and an old "out of labels" mustn't hold a run.
HEALTH_FRESH = timedelta(minutes=2)


def _cant_print(session: Session, printer_id: str) -> str | None:
    """What the printer last said is stopping it, if it said so recently.

    Only an error stops a run. A warning still prints, and a printer that
    doesn't answer at all fails the send, which is what retries are for.
    """
    state, problems, at = session.execute(
        select(PrinterRow.health, PrinterRow.health_problems, PrinterRow.health_at)
        .where(PrinterRow.id == printer_id)).one_or_none() or (None, None, None)
    if state != "error" or at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    if datetime.now(UTC) - at > HEALTH_FRESH:
        return None
    return "; ".join(problems or []) or "it says it can't print"


def _move_requested(session: Session, run_id: str) -> str | None:
    """Read right after `_cancelled`, which has just committed."""
    return session.scalar(select(PrintRun.move_to).where(PrintRun.id == run_id))


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
        if run.move_to:                    # asked to move before it got going
            run.printer_id, run.move_to = run.move_to, None
            s.commit()
        printer = printers.load(s, run.printer_id)
        if printer is None:
            run.status = "failed"
            run.error = f"printer {run.printer_id!r} no longer exists"
            run.finished_at = datetime.now(UTC)
            s.commit()
            events.run_changed(run)
            log.error("run failed: %s", where(run=run_id, printer=run.printer_id,
                                              reason="printer no longer exists"))
            return

        # nothing new starts while the queue is held; resuming puts it back
        if paused(s):
            run.status = "paused"
            s.commit()
            events.run_changed(run)
            log.info("run held before it started: %s", where(run=run_id))
            return

        # claimed, not just set: a retry rq scheduled earlier can fire while
        # this run is already printing (or after it was moved and started
        # again), and two workers on one run would send the same labels twice
        claimed = s.execute(
            update(PrintRun)
            .where(PrintRun.id == run_id, PrintRun.status.in_(STARTABLE))
            .values(status="printing", started_at=datetime.now(UTC), error=None)
        ).rowcount
        s.commit()
        if not claimed:
            s.refresh(run)
            log.info("run not started, it is %s: %s", run.status, where(run=run_id))
            return
        # a move that landed after this run was read but before it said it was
        # printing: from here on the API sees "printing" and asks via move_to
        s.refresh(run)
        if run.printer_id != printer.id:
            moved = printers.load(s, run.printer_id)
            printer = moved if moved is not None else printer
        events.run_changed(run)
        printed = run.total - remaining(s, run_id)
        log.info("run started: %s", where(run=run_id, printer=run.printer_id,
                                          printed=printed, total=run.total,
                                          attempt=run.attempts + 1))
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
                    events.run_changed(run)
                    log.info("run cancelled: %s", where(run=run_id, printed=printed,
                                                        total=run.total))
                    return
                target = _move_requested(s, run_id)
                if target:
                    # between labels, like cancel: the rest of the roll goes
                    # to the new printer, and nothing already out is sent again
                    run.printer_id, run.move_to = target, None
                    run.status, run.printed = "queued", printed
                    s.commit()
                    events.run_changed(run)
                    log.info("run moved: %s", where(run=run_id, to=target, printed=printed,
                                                   total=run.total))
                    enqueue(run_id)
                    return
                reason = _cant_print(s, run.printer_id)
                if reason:
                    # between labels, like cancel: sending more to a printer
                    # with no labels in it only fills its buffer. The health
                    # check puts the run back on the queue when it recovers.
                    run.status, run.printed = "blocked", printed
                    run.error = f"waiting for {printer.name}: {reason}"
                    s.commit()
                    events.run_changed(run)
                    log.info("run waiting for its printer: %s",
                             where(run=run_id, printer=run.printer_id, reason=reason,
                                   printed=printed, total=run.total))
                    return
                if paused(s):
                    # between labels, like cancel: holding halfway through one
                    # would leave it under the print head
                    run.status = "paused"
                    run.printed = printed
                    s.commit()
                    events.run_changed(run)
                    log.info("run held: %s", where(run=run_id, printed=printed,
                                                   total=run.total))
                    return
                # one label per write: the printer buffers a few, and a mid-run
                # failure then costs one label instead of the whole batch
                # a page run carries a PDF per document; a label run, ZPL
                printer.transport.send(label.body if label.body is not None
                                       else label.zpl.encode("ascii") + b"\n")
                label.printed_at = datetime.now(UTC)
                printed += 1
                run.printed = printed
                s.commit()
                events.run_changed(run)
                # hand-fed stock: one label, then wait to be told to carry on.
                # A different kind of stopped from a held queue, so it says so.
                if run.pause_between and printed < run.total:
                    run.status = "waiting"
                    s.commit()
                    events.run_changed(run)
                    log.info("run waiting on the operator: %s",
                             where(run=run_id, printed=printed, total=run.total))
                    return
            run.status, run.move_to = "done", None
            log.info("run done: %s", where(run=run_id, printer=run.printer_id,
                                           printed=printed))
        except Exception as exc:
            run.attempts += 1
            run.printed = printed
            run.error = f"{type(exc).__name__}: {exc}"
            left = getattr(job, "retries_left", 0) or 0
            run.status = "retrying" if left else "failed"
            run.finished_at = None if left else datetime.now(UTC)
            s.commit()
            events.run_changed(run)
            log.error("run %s: %s", run.status,
                      where(run=run_id, printer=run.printer_id, printed=printed,
                            total=run.total, tries_left=left, error=run.error))
            raise                                     # rq schedules the next attempt
        run.finished_at = datetime.now(UTC)
        s.commit()
        events.run_changed(run)
