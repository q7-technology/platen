"""Moving a run to another printer, and detouring a printer's work. The one
rule that matters: a label that has come out is never sent again."""

from __future__ import annotations

import pytest

from app import jobs, printers
from db import session as dbsession
from db.models import PrintRun
from tests.conftest import RecordingTransport
from tests.test_sites import _person

PARAMS = {"despatch_date": "2026-09-18"}


@pytest.fixture
def floor(seeded, monkeypatch) -> dict[str, RecordingTransport]:
    """The seeded dock, plus a spare beside it and a 300 dpi one; each keeps
    its own record of what it was sent."""
    sent = {"dock": RecordingTransport(), "spare": RecordingTransport(),
            "fine": RecordingTransport()}
    monkeypatch.setitem(printers.TRANSPORTS, "memory",
                        lambda cfg, session=None: sent[cfg.get("name", "dock")])
    for pid, dpi in (("spare", 203), ("fine", 300)):
        assert seeded.put(f"/printers/{pid}", json={
            "name": pid.title(), "dpi": dpi,
            "transport": {"kind": "memory", "name": pid}}).status_code == 200
    return sent


def _run(client, printer_id: str = "dock", **extra) -> str:
    r = client.post("/runs", json={"template_id": "carton", "printer_id": printer_id,
                                   "params": PARAMS, **extra})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _status(client, run_id: str) -> dict:
    return client.get(f"/runs/{run_id}").json()


def test_a_queued_run_goes_where_it_was_sent(seeded, floor):
    run_id = _run(seeded)
    r = seeded.post(f"/runs/{run_id}/move", json={"printer_id": "spare"})
    assert r.status_code == 200, r.text
    assert r.json()["from"] == "dock" and r.json()["to"] == "spare"
    jobs.print_run(run_id)
    assert len(floor["spare"].sent) == 12 and floor["dock"].sent == []


def test_a_run_moved_mid_roll_never_prints_a_label_twice(seeded, floor):
    run_id = _run(seeded)

    def move_after_third(n: int) -> None:
        if n == 3:
            assert seeded.post(f"/runs/{run_id}/move",
                               json={"printer_id": "spare"}).json()["moving"] is True

    floor["dock"].after_send = move_after_third
    jobs.print_run(run_id)                       # stops between labels, queues itself again
    st = _status(seeded, run_id)
    assert st["status"] == "queued" and st["printer_id"] == "spare" and st["printed"] == 3
    jobs.print_run(run_id)

    dock, spare = floor["dock"].sent, floor["spare"].sent
    assert len(dock) == 3 and len(spare) == 9
    assert not set(dock) & set(spare)            # not one label in both piles
    st = _status(seeded, run_id)
    assert st["status"] == "done" and st["printed"] == 12 and st["moving_to"] is None


def test_undo_is_moving_it_back(seeded, floor):
    run_id = _run(seeded)
    seeded.post(f"/runs/{run_id}/move", json={"printer_id": "spare"})
    back = seeded.post(f"/runs/{run_id}/move", json={"printer_id": "dock"}).json()
    assert back["from"] == "spare" and back["to"] == "dock"
    jobs.print_run(run_id)
    assert len(floor["dock"].sent) == 12


def test_a_move_asked_for_before_it_started_is_honoured(seeded, floor):
    run_id = _run(seeded)
    with dbsession.SessionLocal() as s:
        s.get(PrintRun, run_id).move_to = "spare"
        s.commit()
    jobs.print_run(run_id)
    assert len(floor["spare"].sent) == 12 and floor["dock"].sent == []


def test_labels_drawn_for_one_dot_pitch_stay_on_it(seeded, floor):
    run_id = _run(seeded)
    r = seeded.post(f"/runs/{run_id}/move", json={"printer_id": "fine"})
    assert r.status_code == 422
    assert "203 dpi" in r.json()["detail"] and "300" in r.json()["detail"]


def test_a_hand_fed_run_moves_only_when_someone_says_so(seeded, floor):
    run_id = _run(seeded, pause_between=True)
    jobs.print_run(run_id)                       # one label, then waits
    assert _status(seeded, run_id)["status"] == "waiting"
    r = seeded.post(f"/runs/{run_id}/move", json={"printer_id": "spare"})
    assert r.status_code == 409 and "feeding stock" in r.json()["detail"]
    r = seeded.post(f"/runs/{run_id}/move", json={"printer_id": "spare", "confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "waiting"
    seeded.post(f"/runs/{run_id}/continue")
    jobs.print_run(run_id)
    assert len(floor["dock"].sent) == 1 and len(floor["spare"].sent) == 1


def test_a_failed_run_moved_goes_back_on_the_queue(seeded, floor, queue):
    run_id = _run(seeded)
    with dbsession.SessionLocal() as s:
        run = s.get(PrintRun, run_id)
        run.status, run.error = "failed", "OSError: no route to host"
        s.commit()
    before = queue.count
    r = seeded.post(f"/runs/{run_id}/move", json={"printer_id": "spare"})
    assert r.json()["status"] == "queued"
    assert queue.count == before + 1
    assert _status(seeded, run_id)["error"] is None


def test_a_finished_run_has_nothing_to_move(seeded, floor):
    run_id = _run(seeded)
    jobs.print_run(run_id)
    assert seeded.post(f"/runs/{run_id}/move", json={"printer_id": "spare"}).status_code == 409


def test_nobody_moves_a_run_to_a_site_they_dont_look_after(seeded, floor):
    for sid in ("bal", "gee"):
        seeded.put(f"/sites/{sid}", json={"name": sid.title()})
    for pid, site in (("dock", "bal"), ("spare", "gee")):
        p = seeded.get(f"/printers/{pid}").json()
        seeded.put(f"/printers/{pid}", json={"name": p["name"], "dpi": p["dpi"],
                                             "transport": p["transport"], "site_id": site})
    sam = _person(seeded, "sam", "operator", ["bal"])
    run_id = _run(sam)
    assert sam.post(f"/runs/{run_id}/move", json={"printer_id": "spare"}).status_code == 403
    mia = _person(seeded, "mia", "manager", ["bal", "gee"])
    assert mia.post(f"/runs/{run_id}/move", json={"printer_id": "spare"}).status_code == 200


# ------------------------------------------------------------------ detours

def test_a_detour_takes_new_and_unfinished_work(seeded, floor):
    waiting = _run(seeded, pause_between=True)
    jobs.print_run(waiting)
    queued = _run(seeded)

    r = seeded.post("/printers/dock/detour", json={"to": "spare"})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == [queued]
    assert r.json()["left"][0]["id"] == waiting          # someone is standing at that one

    made = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                      "params": PARAMS}).json()
    assert made["printer_id"] == "spare" and made["detoured_from"] == "dock"

    assert seeded.post("/printers/dock/detour", json={"to": None}).json()["detour_to"] is None
    assert _run(seeded) and seeded.get("/runs?limit=1").json()[0]["printer_id"] == "dock"


def test_detours_dont_chain_or_loop(seeded, floor):
    assert seeded.post("/printers/dock/detour", json={"to": "dock"}).status_code == 422
    assert seeded.post("/printers/dock/detour", json={"to": "spare"}).status_code == 200
    assert seeded.post("/printers/spare/detour", json={"to": "dock"}).status_code == 409
    assert seeded.post("/printers/fine/detour", json={"to": "dock"}).status_code == 409
    assert seeded.post("/printers/fine/detour", json={"to": "spare"}).status_code == 422  # dpi


def test_a_run_cant_be_moved_onto_a_detoured_printer(seeded, floor):
    run_id = _run(seeded)
    seeded.post("/printers/dock/detour", json={"to": "spare"})   # takes run_id with it
    assert _status(seeded, run_id)["printer_id"] == "spare"
    r = seeded.post(f"/runs/{run_id}/move", json={"printer_id": "dock"})
    assert r.status_code == 409 and "detoured" in r.json()["detail"]


def test_only_a_manager_detours_or_rearranges_a_floor(seeded, floor, operator):
    assert operator.post("/printers/dock/detour", json={"to": "spare"}).status_code == 403
    assert operator.put("/printers/dock/position", json={"x": 2, "y": 3}).status_code == 403
    mia = _person(seeded, "mia", "manager", [])
    assert mia.put("/printers/dock/position", json={"x": 2, "y": 3}).json()["grid"] == [2, 3]
    assert mia.post("/printers/dock/detour", json={"to": "spare"}).status_code == 200


def test_a_run_waiting_out_a_retry_moves_now(seeded, floor, queue):
    run_id = _run(seeded)
    with dbsession.SessionLocal() as s:
        s.get(PrintRun, run_id).status = "retrying"
        s.commit()
    before = queue.count
    r = seeded.post(f"/runs/{run_id}/move", json={"printer_id": "spare"})
    assert r.json()["status"] == "queued" and queue.count == before + 1
    jobs.print_run(run_id)                            # the move's own job
    jobs.print_run(run_id)                            # the old retry, firing late
    assert len(floor["spare"].sent) == 12 and floor["dock"].sent == []


def test_moving_a_printing_run_back_calls_the_move_off(seeded, floor):
    run_id = _run(seeded)

    def change_of_mind(n: int) -> None:
        if n == 2:
            assert seeded.post(f"/runs/{run_id}/move", json={"printer_id": "spare"}).json()["moving"]
            back = seeded.post(f"/runs/{run_id}/move", json={"printer_id": "dock"})
            assert back.status_code == 200 and back.json()["moving"] is False

    floor["dock"].after_send = change_of_mind
    jobs.print_run(run_id)
    assert len(floor["dock"].sent) == 12 and floor["spare"].sent == []
    assert _status(seeded, run_id)["status"] == "done"
