"""Printing one record at a time, for stock somebody is feeding by hand."""

from __future__ import annotations

import pytest

from app import jobs

PARAMS = {"despatch_date": "2026-09-18"}


@pytest.fixture
def stepping(seeded) -> str:
    return seeded.post("/runs", json={
        "template_id": "carton", "printer_id": "dock", "params": PARAMS,
        "pause_between": True}).json()["id"]


def test_it_stops_after_the_first_label(seeded, transport, stepping):
    jobs.print_run(stepping)

    status = seeded.get(f"/runs/{stepping}").json()
    assert len(transport.sent) == 1
    assert status["status"] == "waiting"
    assert status["printed"] == 1


def test_carrying_on_prints_exactly_one_more(seeded, transport, stepping, queue):
    jobs.print_run(stepping)
    transport.sent.clear()

    assert seeded.post(f"/runs/{stepping}/continue").status_code == 202
    jobs.print_run(stepping)

    assert len(transport.sent) == 1
    assert seeded.get(f"/runs/{stepping}").json()["printed"] == 2


def test_the_last_one_finishes_the_run(seeded, transport, stepping):
    for _ in range(12):
        jobs.print_run(stepping)
        seeded.post(f"/runs/{stepping}/continue")

    status = seeded.get(f"/runs/{stepping}").json()
    assert status["status"] == "done"
    assert status["printed"] == 12
    assert len(transport.sent) == 12


def test_letting_the_whole_queue_go_does_not_run_off_with_a_hand_fed_job(seeded, transport, stepping):
    """A held queue and a hand-fed job are different kinds of stopped."""
    jobs.print_run(stepping)

    seeded.post("/queue/resume")

    assert seeded.get(f"/runs/{stepping}").json()["status"] == "waiting"
    assert len(transport.sent) == 1


def test_carrying_on_a_run_that_is_not_waiting_is_refused(seeded, transport):
    run = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                     "params": PARAMS}).json()["id"]
    jobs.print_run(run)

    r = seeded.post(f"/runs/{run}/continue")

    assert r.status_code == 409
    assert "done" in r.json()["detail"]


def test_cancelling_a_hand_fed_job_still_works(seeded, transport, stepping):
    jobs.print_run(stepping)

    seeded.post(f"/runs/{stepping}/cancel")
    seeded.post(f"/runs/{stepping}/continue")
    jobs.print_run(stepping)

    assert seeded.get(f"/runs/{stepping}").json()["status"] == "cancelled"
    assert len(transport.sent) == 1
