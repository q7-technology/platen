"""The job history: what a run looks like from the outside, a week later."""

from __future__ import annotations

from sqlalchemy import event

from db import session as dbsession

PARAMS = {"despatch_date": "2026-09-18"}


def _make_runs(client, n: int) -> list[str]:
    return [client.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                       "params": PARAMS}).json()["id"] for _ in range(n)]


class _Counter:
    """Counts the statements one block of work sends to the database."""

    def __init__(self) -> None:
        self.n = 0

    def __enter__(self) -> "_Counter":
        self.engine = dbsession.engine()
        event.listen(self.engine, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self._hit)

    def _hit(self, *args) -> None:
        self.n += 1


def test_listing_runs_does_not_ask_once_per_run(seeded):
    """A dashboard that issues a query per row falls over on a busy morning."""
    _make_runs(seeded, 8)

    with _Counter() as few:
        rows = seeded.get("/runs").json()

    assert len(rows) == 8
    assert few.n < 8, f"listing 8 runs took {few.n} queries; that is one per run"


def test_a_run_says_which_template_it_printed_by_name(seeded):
    run_id = _make_runs(seeded, 1)[0]

    row = seeded.get(f"/runs/{run_id}").json()

    assert row["template_id"] == "carton"
    assert row["template_name"] == "Carton"
    assert row["template_version"] == 1


def test_runs_come_back_newest_first(seeded):
    made = _make_runs(seeded, 4)

    listed = [r["id"] for r in seeded.get("/runs").json()]

    assert listed == list(reversed(made))


def test_the_list_can_be_narrowed_to_what_went_wrong(seeded, transport):
    from app import jobs
    good = _make_runs(seeded, 1)[0]
    bad = _make_runs(seeded, 1)[0]
    transport.after_send = lambda n: (_ for _ in ()).throw(OSError("media out"))
    try:
        jobs.print_run(bad)
    except OSError:
        pass

    failed = [r["id"] for r in seeded.get("/runs", params={"status": "failed"}).json()]

    assert failed == [bad]
    assert good not in failed


def test_a_runs_warnings_travel_with_it(seeded):
    run_id = _make_runs(seeded, 1)[0]

    row = seeded.get(f"/runs/{run_id}").json()

    assert row["warnings"] == ["row 7: logo: no image in this row"]


def test_the_job_screen_is_served(client):
    r = client.get("/studio/jobs")
    assert r.status_code == 200
    assert "Job history" in r.text
