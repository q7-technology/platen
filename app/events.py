"""What just happened, told to every screen that is watching.

The worker and the API publish a small JSON message on one Redis channel when
a run moves or a printer's health changes. `/events` relays them to browsers
as server-sent events, and only the ones about printers that person can see.

An event is a nudge, not a record: the database is still the truth, and a
screen that misses one catches up on its next read. So publishing never
raises. A Redis that has gone away must not stop a label from printing.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

from db.models import PrintRun

log = logging.getLogger("platen.events")

CHANNEL = "platen:events"
HEARTBEAT = 15.0        # a comment line now and then, so proxies keep the stream open
REFRESH = 30.0          # how often a stream re-reads which printers its viewer may see


def publish(kind: str, **data: Any) -> None:
    from . import jobs  # jobs imports this module

    try:
        jobs.connection().publish(CHANNEL, json.dumps({"kind": kind, **data}, default=str))
    except Exception as exc:
        log.debug("event not published: %s: %s", type(exc).__name__, exc)


def run_changed(run: PrintRun) -> None:
    publish("run", id=run.id, printer_id=run.printer_id, status=run.status,
            printed=run.printed, total=run.total, error=run.error)


def shows(event: dict[str, Any], visible: set[str] | None) -> bool:
    """None means every printer. An event about no printer in particular,
    like the queue being held, is everyone's business."""
    printer_id = event.get("printer_id")
    return visible is None or printer_id is None or printer_id in visible


def stream(visible: Callable[[], set[str] | None], *,
           stop: Callable[[], bool] = lambda: False,
           heartbeat: float = HEARTBEAT, refresh: float = REFRESH) -> Iterator[str]:
    """Server-sent events for one viewer, until `stop` says so or the browser
    goes away (the next write fails and the generator is closed)."""
    from . import jobs

    pubsub = jobs.connection().pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(CHANNEL)
    try:
        allowed = visible()
        checked = last_sent = time.monotonic()
        yield "retry: 3000\n\n"
        while not stop():
            message = pubsub.get_message(timeout=1.0)
            now = time.monotonic()
            if now - checked >= refresh:
                # someone may have been moved to another site since this opened
                allowed, checked = visible(), now
            if message and message.get("type") == "message":
                try:
                    event = json.loads(message["data"])
                except (TypeError, ValueError):
                    continue
                if shows(event, allowed):
                    yield f"event: {event.get('kind', 'message')}\ndata: {json.dumps(event)}\n\n"
                    last_sent = now
            elif now - last_sent >= heartbeat:
                yield ": still here\n\n"
                last_sent = now
    finally:
        pubsub.close()
