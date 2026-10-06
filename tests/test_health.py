"""Printer health: what a Zebra says about itself, kept and announced."""

from __future__ import annotations

import json

import pytest

from app import health, jobs
from db import session as dbsession
from db.models import Printer
from tests.test_transports import Listener, _closed_port

OUT_AND_OPEN = b"\x02  PRINTER STATUS\r\n   ERRORS:   1 00000000 00000005\r\n   WARNINGS: 0 00000000 00000000\x03"
NEARLY_OUT = b"\x02  PRINTER STATUS\r\n   ERRORS:   0 00000000 00000000\r\n   WARNINGS: 1 00000000 00000008\x03"
ALL_GOOD = b"\x02  PRINTER STATUS\r\n   ERRORS:   0 00000000 00000000\r\n   WARNINGS: 0 00000000 00000000\x03"


def test_errors_are_named():
    h = health.read_hqes(OUT_AND_OPEN.decode())
    assert h == health.Health("error", ("out of labels", "head open"))


def test_a_warning_still_prints():
    assert health.read_hqes(NEARLY_OUT.decode()) == health.Health("warning", ("labels nearly out",))


def test_nothing_wrong_is_ready():
    assert health.read_hqes(ALL_GOOD.decode()) == health.Health("ready")


def test_a_flag_nobody_named_is_still_reported():
    h = health.read_hqes("ERRORS: 1 00000000 00000100")
    assert h is not None and h.state == "error"
    assert h.problems == ("errors flag 0x100",)


def test_a_reply_that_isnt_hqes_is_none():
    assert health.read_hqes("") is None
    assert health.read_hqes("ZD621-203dpi,V84.20.18Z") is None


def test_asking_a_real_socket():
    talking = Listener(reply=OUT_AND_OPEN)
    try:
        from app import printers
        h = health.check(printers.RawTcp("127.0.0.1", talking.port))
    finally:
        talking.close()
    assert h.state == "error" and "out of labels" in h.problems


def test_a_printer_that_is_not_there_is_offline():
    from app import printers
    assert health.check(printers.RawTcp("127.0.0.1", _closed_port())).state == "offline"


@pytest.fixture
def heard(queue):
    """Everything published while the test runs."""
    pubsub = jobs.connection().pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe("platen:events")

    def drain() -> list[dict]:
        # a None can be the subscribe confirmation being skipped, so stop only
        # after a few quiet reads in a row
        out, quiet = [], 0
        while quiet < 3:
            m = pubsub.get_message(timeout=0.05)
            if m is None:
                quiet += 1
            elif m.get("type") == "message":
                out.append(json.loads(m["data"]))
                quiet = 0
        return out
    return drain


def test_a_change_is_kept_and_announced_once(seeded, heard):
    talking = Listener(reply=OUT_AND_OPEN)
    try:
        assert seeded.put("/printers/dock2", json={
            "name": "Dock 2", "transport": {"kind": "tcp", "host": "127.0.0.1",
                                            "port": talking.port}}).status_code == 200
        with dbsession.SessionLocal() as s:
            changed = health.check_all(s)
        assert set(changed) == {"dock", "dock2"}
        with dbsession.SessionLocal() as s:
            assert health.check_all(s) == []          # nothing new, nothing said
    finally:
        talking.close()

    said = [e for e in heard() if e["kind"] == "printer"]
    assert len(said) == 2
    dock2 = next(e for e in said if e["printer_id"] == "dock2")
    assert dock2["health"] == "error" and dock2["problems"] == ["out of labels", "head open"]

    shown = {p["id"]: p["health"] for p in seeded.get("/printers?probe=false").json()}
    assert shown["dock2"]["state"] == "error"
    assert shown["dock"]["state"] == "ready"
    with dbsession.SessionLocal() as s:
        assert s.get(Printer, "dock2").health_at is not None
