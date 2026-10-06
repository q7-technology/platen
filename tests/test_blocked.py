"""A printer that says it can't print holds its runs, and lets them go again
when it can. Nothing is sent to a printer with no labels in it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app import health, jobs
from db import session as dbsession
from db.models import Printer

PARAMS = {"despatch_date": "2026-09-18"}


def _say(printer_id: str, state: str, problems: list[str], ago: timedelta = timedelta(0)) -> None:
    with dbsession.SessionLocal() as s:
        row = s.get(Printer, printer_id)
        row.health, row.health_problems = state, problems
        row.health_at = datetime.now(UTC) - ago
        s.commit()


def _run(client) -> str:
    return client.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                      "params": PARAMS}).json()["id"]


def test_a_printer_out_of_labels_is_sent_nothing(seeded, transport):
    _say("dock", "error", ["out of labels"])
    run_id = _run(seeded)
    jobs.print_run(run_id)
    st = seeded.get(f"/runs/{run_id}").json()
    assert transport.sent == []
    assert st["status"] == "blocked" and "out of labels" in st["error"]


def test_it_stops_between_labels_when_the_roll_runs_out(seeded, transport):
    run_id = _run(seeded)
    transport.after_send = lambda n: _say("dock", "error", ["out of labels"]) if n == 3 else None
    jobs.print_run(run_id)
    st = seeded.get(f"/runs/{run_id}").json()
    assert len(transport.sent) == 3 and st["printed"] == 3 and st["status"] == "blocked"


def test_a_warning_still_prints(seeded, transport):
    _say("dock", "warning", ["labels nearly out"])
    run_id = _run(seeded)
    jobs.print_run(run_id)
    assert len(transport.sent) == 12


def test_an_old_answer_holds_nothing(seeded, transport):
    _say("dock", "error", ["head open"], ago=timedelta(minutes=10))
    run_id = _run(seeded)
    jobs.print_run(run_id)
    assert len(transport.sent) == 12


def test_the_run_carries_on_when_the_printer_recovers(seeded, transport, queue):
    run_id = _run(seeded)
    transport.after_send = lambda n: _say("dock", "error", ["out of labels"]) if n == 5 else None
    jobs.print_run(run_id)
    transport.after_send = None
    before = queue.count
    with dbsession.SessionLocal() as s:
        assert "dock" in health.check_all(s)      # the memory printer answers: ready
    assert queue.count == before + 1
    st = seeded.get(f"/runs/{run_id}").json()
    assert st["status"] == "queued" and st["error"] is None
    jobs.print_run(run_id)
    assert len(transport.sent) == 12              # 5, then the 7 left; none twice
    assert len(set(transport.sent)) == 12
    assert seeded.get(f"/runs/{run_id}").json()["status"] == "done"


def test_a_waiting_run_can_be_cancelled_straight_away(seeded, transport):
    _say("dock", "error", ["out of labels"])
    run_id = _run(seeded)
    jobs.print_run(run_id)
    assert seeded.post(f"/runs/{run_id}/cancel").json()["status"] == "cancelled"
    assert seeded.get(f"/runs/{run_id}").json()["status"] == "cancelled"


def test_a_waiting_run_can_be_moved_to_a_printer_that_works(seeded, transport, queue):
    seeded.put("/printers/spare", json={"name": "Spare", "transport": {"kind": "memory"}})
    _say("dock", "error", ["head open"])
    run_id = _run(seeded)
    jobs.print_run(run_id)
    r = seeded.post(f"/runs/{run_id}/move", json={"printer_id": "spare"})
    assert r.status_code == 200 and r.json()["status"] == "queued"
