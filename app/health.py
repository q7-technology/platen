"""How each printer is, asked on a timer rather than when somebody looks.

A Zebra on a socket answers ~HQES with two sets of flags: errors (it can't
print) and warnings (it can, but someone should look soon). Anything else, a
CUPS queue, an agent, a printer that ignores ~HQES, gets the only answer
there is: it answered, or it didn't.

States, worst first: offline, error, warning, ready. A change is written to
the printer's row and published, so the screens that are watching hear about
"out of labels" within one check of it happening.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Printer as PrinterRow

from . import events, printers
from .logs import where

log = logging.getLogger("platen.health")

EVERY = 30.0             # seconds between checks

# The low eight bits of each ~HQES group, as the ZPL manual numbers them.
ERRORS = {
    0x01: "out of labels",
    0x02: "out of ribbon",
    0x04: "head open",
    0x08: "cutter fault",
    0x10: "print head too hot",
    0x20: "motor too hot",
    0x40: "bad print head element",
    0x80: "print head not detected",
}
WARNINGS = {
    0x01: "needs calibrating",
    0x02: "print head needs cleaning",
    0x04: "print head needs replacing",
    0x08: "labels nearly out",
}

_GROUP = r"{}:\s*([01])\s+([0-9A-Fa-f]{{8}})\s+([0-9A-Fa-f]{{8}})"


@dataclass(frozen=True)
class Health:
    state: str                        # ready | warning | error | offline
    problems: tuple[str, ...] = ()


def _flags(text: str, label: str, names: dict[int, str]) -> list[str]:
    m = re.search(_GROUP.format(label), text)
    if m is None or m.group(1) == "0":
        return []
    bits = int(m.group(3), 16)
    found = [name for bit, name in names.items() if bits & bit]
    return found or [f"{label.lower()} flag {bits:#x}"]   # one this table doesn't name


def read_hqes(text: str) -> Health | None:
    """None when the reply isn't a ~HQES answer at all."""
    if "ERRORS:" not in text:
        return None
    errors = _flags(text, "ERRORS", ERRORS)
    if errors:
        return Health("error", tuple(errors))
    warnings = _flags(text, "WARNINGS", WARNINGS)
    return Health("warning", tuple(warnings)) if warnings else Health("ready")


def check(transport: printers.Transport) -> Health:
    ask = getattr(transport, "status", None)
    if ask is None:
        return Health("ready") if transport.probe() else Health("offline", ("not answering",))
    try:
        reply = ask()
    except OSError:
        return Health("offline", ("not answering",))
    # it took the connection; one that won't say how it is still printed fine
    return read_hqes(reply) or Health("ready")


def _safe_check(row: PrinterRow) -> Health:
    try:
        transport = printers.from_row(row).transport
        if row.kind == "page":
            # ~HQES is ZPL; an office printer on 9100 would print it on a
            # sheet of paper. Whether it answers is all it is asked.
            return Health("ready") if transport.probe() else Health("offline", ("not answering",))
        return check(transport)
    except Exception as exc:
        return Health("offline", (f"{type(exc).__name__}: {exc}",))


def check_all(s: Session) -> list[str]:
    """Check every printer, record what changed, and say so. Returns the ids
    that changed. Socket printers are asked all at once; an agent's answer is
    a database read and needs this session, so those are done here."""
    rows = list(s.scalars(select(PrinterRow)))
    agents = [r for r in rows if r.transport_kind == "agent"]
    others = [r for r in rows if r.transport_kind != "agent"]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(others)))) as pool:
        results = dict(zip([r.id for r in others], pool.map(_safe_check, others), strict=True))
    for row in agents:
        transport = printers.from_row(row, s).transport
        results[row.id] = (Health("ready") if transport.probe()
                           else Health("offline", ("agent not checked in",)))

    now = datetime.now(UTC)
    changed = []
    for row in rows:
        h = results[row.id]
        row.health_at = now
        if row.health != h.state or list(row.health_problems or []) != list(h.problems):
            row.health, row.health_problems = h.state, list(h.problems)
            changed.append(row)
    s.commit()
    for row in changed:
        level = logging.INFO if row.health in ("ready", "warning") else logging.WARNING
        log.log(level, "printer is %s: %s", row.health,
                where(printer=row.id, problems="; ".join(row.health_problems) or "none"))
        events.publish("printer", printer_id=row.id, site_id=row.site_id,
                       health=row.health, problems=row.health_problems, checked_at=now)
    return [r.id for r in changed]
