"""The dashboard, and the one control on it that needed building: pause.

Pausing reuses what a retry already does. A paused run stops between labels
and resumes where it stopped, because that is the only way to pause a printer
without printing a consignment barcode twice.
"""

from __future__ import annotations

import pytest

from app import jobs

PARAMS = {"despatch_date": "2026-09-18"}


def _run(client) -> str:
    return client.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                      "params": PARAMS}).json()["id"]


# ----------------------------------------------------------------- the tiles

def test_the_dashboard_counts_what_has_printed_today(seeded, transport):
    run = _run(seeded)
    jobs.print_run(run)

    body = seeded.get("/dashboard").json()

    assert body["printed_today"] == 12
    assert body["queued"] == 0
    assert body["failed_today"] == 0


def test_it_counts_what_is_still_waiting(seeded):
    _run(seeded)
    _run(seeded)

    body = seeded.get("/dashboard").json()

    assert body["queued"] == 2
    assert body["printed_today"] == 0


def test_it_counts_what_went_wrong(seeded, transport):
    run = _run(seeded)
    transport.after_send = lambda n: (_ for _ in ()).throw(OSError("media out"))
    with pytest.raises(OSError):
        jobs.print_run(run)

    body = seeded.get("/dashboard").json()

    assert body["failed_today"] == 1
    assert "media out" in body["runs"][0]["error"]


def test_it_says_how_many_printers_are_answering(seeded):
    body = seeded.get("/dashboard").json()

    assert body["printers"]["total"] == 1
    assert body["printers"]["online"] == 1


def test_it_lists_the_templates_most_recently_worked_on(seeded):
    body = seeded.get("/dashboard").json()

    assert [t["id"] for t in body["templates"]] == ["carton"]
    assert body["templates"][0]["version"] == 1


def test_an_operator_may_see_the_dashboard(operator):
    assert operator.get("/dashboard").status_code == 200


# --------------------------------------------------------------------- pause

def test_the_queue_starts_running(seeded):
    assert seeded.get("/dashboard").json()["paused"] is False


def test_pausing_stops_a_run_between_labels(seeded, transport):
    run = _run(seeded)
    transport.after_send = lambda n: seeded.post("/queue/pause") if n == 4 else None

    jobs.print_run(run)

    status = seeded.get(f"/runs/{run}").json()
    assert status["status"] == "paused"
    assert status["printed"] == 4
    assert len(transport.sent) == 4


def test_resuming_picks_up_where_it_stopped(seeded, transport, queue):
    run = _run(seeded)
    transport.after_send = lambda n: seeded.post("/queue/pause") if n == 4 else None
    jobs.print_run(run)
    transport.after_send = None
    transport.sent.clear()

    seeded.post("/queue/resume")
    jobs.print_run(run)

    assert len(transport.sent) == 8, "resuming reprinted labels that already came out"
    assert seeded.get(f"/runs/{run}").json()["status"] == "done"


def test_resuming_puts_the_paused_runs_back_on_the_queue(seeded, transport, queue):
    run = _run(seeded)
    transport.after_send = lambda n: seeded.post("/queue/pause") if n == 2 else None
    jobs.print_run(run)
    waiting = queue.count

    body = seeded.post("/queue/resume").json()

    assert body["resumed"] == 1
    assert queue.count == waiting + 1
    assert seeded.get(f"/runs/{run}").json()["status"] == "queued"


def test_nothing_new_starts_while_it_is_paused(seeded, transport):
    seeded.post("/queue/pause")
    run = _run(seeded)

    jobs.print_run(run)

    assert transport.sent == []
    assert seeded.get(f"/runs/{run}").json()["status"] == "paused"


def test_only_an_administrator_may_pause_everything(operator):
    assert operator.post("/queue/pause").status_code == 403
    assert operator.post("/queue/resume").status_code == 403


def test_the_dashboard_screen_is_served(client):
    r = client.get("/studio/dashboard")
    assert r.status_code == 200
    assert "Dashboard" in r.text
