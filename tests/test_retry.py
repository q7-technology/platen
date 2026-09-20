"""A run that fails part way, and what happens next.

The rule that matters: a retry resumes, it never restarts. Reprinting a
consignment barcode that already went on a carton is worse than not printing
it at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import jobs
from db import session as dbsession
from db.models import RunLabel

PARAMS = {"despatch_date": "2026-09-18"}


@pytest.fixture
def run_id(seeded) -> str:
    return seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                      "params": PARAMS}).json()["id"]


def fail_on(transport, n: int) -> None:
    def boom(count: int) -> None:
        if count == n:
            raise OSError("the printer stopped answering")
    transport.after_send = boom


def _printed(run_id: str) -> list[int]:
    with dbsession.SessionLocal() as s:
        return [label.seq for label in s.scalars(
            select(RunLabel).where(RunLabel.run_id == run_id, RunLabel.printed_at.isnot(None))
            .order_by(RunLabel.seq))]


def test_a_printer_that_stops_answering_leaves_the_run_where_it_stopped(seeded, transport, run_id):
    fail_on(transport, 5)

    with pytest.raises(OSError):
        jobs.print_run(run_id)

    status = seeded.get(f"/runs/{run_id}").json()
    assert status["printed"] == 4
    assert status["total"] == 12
    assert "stopped answering" in status["error"]
    assert _printed(run_id) == [1, 2, 3, 4]


def test_a_retry_prints_only_what_is_left(seeded, transport, run_id):
    fail_on(transport, 5)
    with pytest.raises(OSError):
        jobs.print_run(run_id)
    transport.after_send = None
    transport.sent.clear()

    jobs.print_run(run_id)

    assert len(transport.sent) == 8, "a retry reprinted labels that already came out"
    assert _printed(run_id) == list(range(1, 13))
    assert seeded.get(f"/runs/{run_id}").json()["status"] == "done"
    assert seeded.get(f"/runs/{run_id}").json()["printed"] == 12


def test_a_cancelled_run_resumes_where_the_operator_stopped_it(seeded, transport, run_id):
    transport.after_send = lambda n: seeded.post(f"/runs/{run_id}/cancel") if n == 3 else None
    jobs.print_run(run_id)
    assert seeded.get(f"/runs/{run_id}").json()["status"] == "cancelled"
    transport.after_send = None
    transport.sent.clear()

    seeded.post(f"/runs/{run_id}/retry")
    jobs.print_run(run_id)

    assert len(transport.sent) == 9
    assert seeded.get(f"/runs/{run_id}").json()["status"] == "done"


def test_the_worker_counts_its_attempts(seeded, transport, run_id):
    for _ in range(2):
        transport.sent.clear()                        # fail on this attempt's second label
        fail_on(transport, 2)
        with pytest.raises(OSError):
            jobs.print_run(run_id)

    assert seeded.get(f"/runs/{run_id}").json()["attempts"] == 2
    assert _printed(run_id) == [1, 2], "each attempt got one more label out"


def test_a_finished_run_is_not_retried(seeded, transport, run_id):
    jobs.print_run(run_id)

    r = seeded.post(f"/runs/{run_id}/retry")

    assert r.status_code == 409
    assert "done" in r.json()["detail"]


def test_retrying_puts_the_job_back_on_the_queue(seeded, transport, queue, run_id):
    fail_on(transport, 5)
    with pytest.raises(OSError):
        jobs.print_run(run_id)
    queued = queue.count

    r = seeded.post(f"/runs/{run_id}/retry")

    assert r.status_code == 202
    assert r.json()["remaining"] == 8
    assert queue.count == queued + 1
    assert seeded.get(f"/runs/{run_id}").json()["status"] == "queued"


def test_a_queued_run_asks_the_worker_to_try_again_by_itself(seeded, queue, run_id):
    """The claim is that runs are retried, so the job has to carry that."""
    job = queue.jobs[0]

    assert job.retries_left and job.retries_left >= 1
    assert job.retry_intervals


def test_a_cancelled_run_that_is_retried_clears_the_cancel(seeded, transport, run_id):
    transport.after_send = lambda n: seeded.post(f"/runs/{run_id}/cancel") if n == 2 else None
    jobs.print_run(run_id)
    transport.after_send = None

    seeded.post(f"/runs/{run_id}/retry")
    jobs.print_run(run_id)

    assert seeded.get(f"/runs/{run_id}").json()["status"] == "done"
