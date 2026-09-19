"""The queue. By the time a job gets here every label is already rendered, so
the worker does nothing that can fail for an interesting reason."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

import redis
from rq import Queue

r = redis.Redis()
queue = Queue("platen", connection=r)

Status = Literal["queued", "printing", "done", "failed", "cancelled"]


@dataclass
class Run:
    id: str
    template_id: str
    printer_id: str
    labels: list[str]              # rendered ZPL, one entry per physical label
    status: Status = "queued"
    printed: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def total(self) -> int:
        return len(self.labels)


def _key(run_id: str) -> str:
    return f"platen:run:{run_id}"


def save(run: Run) -> None:
    r.set(_key(run.id), json.dumps(asdict(run)), ex=7 * 24 * 3600)


def load(run_id: str) -> Run:
    raw = r.get(_key(run_id))
    if raw is None:
        raise KeyError(run_id)
    return Run(**json.loads(raw))


def cancel(run_id: str) -> None:
    r.set(f"platen:cancel:{run_id}", "1", ex=3600)


def enqueue(run: Run) -> None:
    save(run)
    queue.enqueue(print_run, run.id, job_timeout=3600, retry=None)


def print_run(run_id: str) -> None:
    """Runs in the worker process."""
    from .printers import PRINTERS

    run = load(run_id)
    printer = PRINTERS[run.printer_id]
    run.status = "printing"
    save(run)

    try:
        for i, zpl in enumerate(run.labels, start=1):
            if r.get(f"platen:cancel:{run_id}"):
                run.status = "cancelled"
                save(run)
                return
            # one label per write: the printer buffers a few, and a mid-run
            # failure then costs one label instead of the whole batch
            printer.transport.send(zpl.encode("ascii") + b"\n")
            run.printed = i
            if i % 5 == 0 or i == run.total:
                save(run)
        run.status = "done"
    except Exception as exc:                          # noqa: BLE001 — recorded, not swallowed
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        save(run)
