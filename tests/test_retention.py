"""Keeping the database from growing forever.

A five thousand label run writes about twenty megabytes of ZPL. Nobody reads
last April's label data, but everybody wants to know what happened, so the
labels go first and the run's history stays.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app import retention
from db import session as dbsession
from db.models import AgentJob, AuditLog, PrintRun, RunLabel, RunWarning, UserSession

PARAMS = {"despatch_date": "2026-09-18"}


def _run(client, status: str = "done", age_days: float = 0) -> str:
    run_id = client.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                        "params": PARAMS}).json()["id"]
    with dbsession.SessionLocal() as s:
        row = s.get(PrintRun, run_id)
        row.status = status
        when = retention.now() - timedelta(days=age_days)
        row.created_at = when
        row.finished_at = when if status in ("done", "failed", "cancelled") else None
        if status == "done":
            row.printed = row.total
        s.commit()
    return run_id


def _counts(run_id: str) -> tuple[int, int, bool]:
    with dbsession.SessionLocal() as s:
        labels = len(s.scalars(select(RunLabel).where(RunLabel.run_id == run_id)).all())
        warnings = len(s.scalars(select(RunWarning).where(RunWarning.run_id == run_id)).all())
        return labels, warnings, s.get(PrintRun, run_id) is not None


# ------------------------------------------------------------------- labels

def test_a_recent_run_keeps_everything(seeded):
    run = _run(seeded, age_days=1)

    with dbsession.SessionLocal() as s:
        retention.prune(s)

    assert _counts(run) == (12, 1, True)


def test_an_old_run_loses_its_labels_but_not_its_story(seeded):
    """What happened is worth keeping. The ZPL is not."""
    run = _run(seeded, age_days=30)

    with dbsession.SessionLocal() as s:
        removed = retention.prune(s)

    labels, warnings, exists = _counts(run)
    assert labels == 0
    assert (warnings, exists) == (1, True)
    assert removed["labels"] == 12
    assert seeded.get(f"/runs/{run}").json()["printed"] == 12


def test_a_very_old_run_goes_altogether(seeded):
    run = _run(seeded, age_days=400)

    with dbsession.SessionLocal() as s:
        removed = retention.prune(s)

    assert _counts(run) == (0, 0, False)
    assert removed["runs"] == 1
    assert seeded.get(f"/runs/{run}").status_code == 404


def test_a_run_that_has_not_finished_is_never_touched(seeded):
    """Age is no reason to throw away a job that might still print."""
    for status in ("queued", "printing", "paused", "waiting", "retrying", "failed"):
        run = _run(seeded, status=status, age_days=400)
        with dbsession.SessionLocal() as s:
            retention.prune(s)
        assert _counts(run) == (12, 1, True), f"a {status} run was pruned"


# -------------------------------------------------------------- other tables

def test_old_audit_entries_go(seeded):
    with dbsession.SessionLocal() as s:
        s.add(AuditLog(action="publish", entity="template", entity_id="old", detail={},
                       at=retention.now() - timedelta(days=400)))
        s.commit()
        removed = retention.prune(s)

    assert removed["audit"] == 1
    with dbsession.SessionLocal() as s:
        assert not [a for a in s.scalars(select(AuditLog)) if a.entity_id == "old"]


def test_sessions_nobody_came_back_for_are_cleared_out(seeded, users):
    with dbsession.SessionLocal() as s:
        s.add(UserSession(id="stale", username="dave",
                          created_at=retention.now() - timedelta(days=30),
                          expires_at=retention.now() - timedelta(days=29),
                          last_seen_at=retention.now() - timedelta(days=29)))
        s.add(UserSession(id="live", username="dave",
                          created_at=retention.now(),
                          expires_at=retention.now() + timedelta(hours=12),
                          last_seen_at=retention.now()))
        s.commit()
        removed = retention.prune(s)

    assert removed["sessions"] == 1
    with dbsession.SessionLocal() as s:
        left = {r.id for r in s.scalars(select(UserSession))}
    assert "stale" not in left
    assert "live" in left, "a session still in use was thrown away"


def test_labels_an_agent_has_finished_with_do_not_pile_up(seeded):
    with dbsession.SessionLocal() as s:
        from db.models import PrintAgent
        s.add(PrintAgent(id="wks", name="Workstation", devices=[]))
        s.add(AgentJob(id="old", agent_id="wks", device="d", zpl="^XA^XZ",
                       created_at=retention.now() - timedelta(days=30),
                       done_at=retention.now() - timedelta(days=30)))
        s.add(AgentJob(id="waiting", agent_id="wks", device="d", zpl="^XA^XZ",
                       created_at=retention.now() - timedelta(days=30)))
        s.commit()
        removed = retention.prune(s)

    assert removed["agent_jobs"] == 1
    with dbsession.SessionLocal() as s:
        assert [j.id for j in s.scalars(select(AgentJob))] == ["waiting"], \
            "a label the agent never collected is not rubbish"


# ------------------------------------------------------------------ settings

def test_how_long_to_keep_things_can_be_changed(seeded, client):
    run = _run(seeded, age_days=30)
    client.put("/maintenance/retention", json={"labels_days": 60})

    with dbsession.SessionLocal() as s:
        retention.prune(s)

    assert _counts(run)[0] == 12, "the longer window was ignored"


def test_keeping_labels_forever_is_allowed_but_deliberate(seeded, client):
    run = _run(seeded, age_days=4000)
    client.put("/maintenance/retention", json={"labels_days": 0, "runs_days": 0})

    with dbsession.SessionLocal() as s:
        retention.prune(s)

    assert _counts(run) == (12, 1, True)


def test_the_settings_come_back_with_their_defaults(client):
    body = client.get("/maintenance/retention").json()

    assert body["labels_days"] == retention.DEFAULTS["labels_days"]
    assert body["runs_days"] == retention.DEFAULTS["runs_days"]


def test_an_administrator_can_run_it_by_hand(seeded, client):
    _run(seeded, age_days=30)

    body = client.post("/maintenance/prune").json()

    assert body["removed"]["labels"] == 12
    assert body["ran_at"]


def test_an_operator_cannot(operator):
    assert operator.post("/maintenance/prune").status_code == 403
    assert operator.put("/maintenance/retention", json={"labels_days": 1}).status_code == 403


# ------------------------------------------------------------- the dry run

def test_a_dry_run_says_what_would_go_and_takes_nothing(seeded):
    run = _run(seeded, age_days=30)

    with dbsession.SessionLocal() as s:
        would = retention.would_remove(s)

    assert would["labels"] == 12
    assert _counts(run) == (12, 1, True), "a dry run deleted something"


def test_the_dry_run_and_the_real_one_agree(seeded):
    _run(seeded, age_days=30)
    _run(seeded, age_days=400)

    with dbsession.SessionLocal() as s:
        would = retention.would_remove(s)
        did = retention.prune(s)

    assert would["runs"] == did["runs"]
    assert would["labels"] == did["labels"]


def test_a_dry_run_on_a_tidy_database_finds_nothing(seeded):
    _run(seeded, age_days=1)

    with dbsession.SessionLocal() as s:
        assert not any(retention.would_remove(s).values())


# ---------------------------------------------------------- doing it by itself

def test_it_is_due_once_a_day_and_not_twice(seeded):
    with dbsession.SessionLocal() as s:
        assert retention.due(s) is True
        assert retention.due(s) is False, "two processes would both have pruned"


def test_it_comes_due_again_once_the_window_has_passed(seeded):
    from datetime import timedelta

    with dbsession.SessionLocal() as s:
        retention.due(s)
        assert retention.due(s, every=timedelta(seconds=0)) is True
