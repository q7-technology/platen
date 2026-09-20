"""What Platen says about itself when nobody is watching.

When a run fails at three in the morning the only record was a column on a
row. There has to be something to grep.
"""

from __future__ import annotations

import logging

import pytest

from app import auth, jobs, logs

PARAMS = {"despatch_date": "2026-09-18"}


def _messages(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# -------------------------------------------------------------------- setup

def test_the_level_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("PLATEN_LOG_LEVEL", "warning")

    assert logs.level_from_env() == logging.WARNING


def test_a_level_nobody_recognises_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("PLATEN_LOG_LEVEL", "chatty")

    assert logs.level_from_env() == logging.INFO


# ------------------------------------------------------------------- the job

def test_a_finished_run_says_so(seeded, transport, caplog):
    run = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                     "params": PARAMS}).json()["id"]

    with caplog.at_level(logging.INFO, logger="platen.jobs"):
        jobs.print_run(run)

    text = _messages(caplog)
    assert run in text
    assert "dock" in text
    assert "12" in text


def test_a_printer_that_stops_answering_is_named_in_the_log(seeded, transport, caplog):
    run = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                     "params": PARAMS}).json()["id"]
    transport.after_send = lambda n: (_ for _ in ()).throw(OSError("media out"))

    with caplog.at_level(logging.INFO, logger="platen.jobs"), pytest.raises(OSError):
        jobs.print_run(run)

    text = _messages(caplog)
    assert "media out" in text
    assert "dock" in text
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_a_run_that_is_held_is_worth_a_line_too(seeded, transport, caplog):
    run = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                     "params": PARAMS}).json()["id"]
    transport.after_send = lambda n: seeded.post("/queue/pause") if n == 3 else None

    with caplog.at_level(logging.INFO, logger="platen.jobs"):
        jobs.print_run(run)

    assert "held" in _messages(caplog).lower()


# ------------------------------------------------------------------ sign-ins

def test_a_refused_sign_in_is_recorded(anon, users, caplog):
    with caplog.at_level(logging.INFO, logger="platen.auth"):
        anon.post("/auth/login", json={"username": "dave", "password": "not-the-password"})

    text = _messages(caplog)
    assert "dave" in text
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_a_password_never_appears_in_the_log(anon, users, caplog):
    with caplog.at_level(logging.DEBUG):
        anon.post("/auth/login", json={"username": "dave", "password": "hunter2-is-secret"})
        anon.post("/auth/login", json={"username": "dave", "password": "dave-password"})

    assert "hunter2-is-secret" not in _messages(caplog)
    assert "dave-password" not in _messages(caplog)


def test_locking_an_account_is_worth_shouting_about(anon, users, caplog):
    with caplog.at_level(logging.INFO, logger="platen.auth"):
        for _ in range(auth.MAX_FAILURES):
            anon.post("/auth/login", json={"username": "dave", "password": "wrong"})

    text = _messages(caplog).lower()
    assert "locked" in text
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_a_key_is_never_written_down_when_it_is_made(client, users, caplog):
    with caplog.at_level(logging.DEBUG):
        token = client.post("/keys", json={"name": "nightly", "role": "operator"}).json()["token"]

    assert token not in _messages(caplog)


# --------------------------------------------------------------------- data

def test_a_query_that_fails_says_which_one(seeded, caplog):
    seeded.put("/queries/cartons", json={
        "datasource_id": "warehouse", "name": "Cartons", "sql": "select * from no_such_table"})

    with caplog.at_level(logging.INFO, logger="platen.datasources"):
        seeded.get("/queries/cartons/columns")

    assert "cartons" in _messages(caplog)
