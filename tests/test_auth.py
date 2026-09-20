"""Who may do what.

Two roles, because that is what the floor looks like: administrators wire up
connections, printers and templates; operators answer a question and press
print. An operator never needs to see a connection string.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import auth
from app.main import app
from db import session as dbsession
from db.models import AppUser

PARAMS = {"despatch_date": "2026-09-18"}


# ------------------------------------------------------------------- basics

def test_a_password_is_never_stored_as_itself(db):
    auth.create_user(dbsession.SessionLocal(), "dave", "correct horse battery", role="operator")

    with dbsession.SessionLocal() as s:
        stored = s.get(AppUser, "dave").password

    assert "correct horse battery" not in stored
    assert stored.startswith("scrypt$")


def test_two_people_with_the_same_password_do_not_share_a_hash(db):
    with dbsession.SessionLocal() as s:
        auth.create_user(s, "a", "same password", role="operator")
        auth.create_user(s, "b", "same password", role="operator")
        assert s.get(AppUser, "a").password != s.get(AppUser, "b").password


def test_a_wrong_password_is_refused(anon, users):
    r = anon.post("/auth/login", json={"username": "dave", "password": "not it"})

    assert r.status_code == 401
    assert "username or password" in r.json()["detail"].lower()
    assert not r.cookies


def test_an_unknown_username_fails_the_same_way_as_a_wrong_password(anon, users):
    unknown = anon.post("/auth/login", json={"username": "nobody", "password": "x"})
    wrong = anon.post("/auth/login", json={"username": "dave", "password": "x"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_logging_in_sets_a_cookie_the_page_cannot_read(anon, users):
    r = anon.post("/auth/login", json={"username": "dave", "password": "dave-password"})

    assert r.status_code == 200
    assert r.json()["role"] == "operator"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_logging_out_puts_the_session_beyond_use(anon, users):
    anon.post("/auth/login", json={"username": "dave", "password": "dave-password"})
    assert anon.get("/auth/me").status_code == 200

    anon.post("/auth/logout")

    assert anon.get("/auth/me").status_code == 401


def test_a_session_that_has_run_out_is_refused(anon, users):
    anon.post("/auth/login", json={"username": "dave", "password": "dave-password"})
    with dbsession.SessionLocal() as s:
        for row in s.scalars(select(auth.UserSession)):
            row.expires_at = auth.now() - auth.timedelta(minutes=1)
        s.commit()

    assert anon.get("/auth/me").status_code == 401


def test_guessing_at_a_password_gets_shut_out(anon, users):
    for _ in range(auth.MAX_FAILURES):
        anon.post("/auth/login", json={"username": "dave", "password": "wrong"})

    r = anon.post("/auth/login", json={"username": "dave", "password": "dave-password"})

    assert r.status_code == 429
    assert "locked" in r.json()["detail"].lower()


# -------------------------------------------------------------- the gate

def test_nothing_useful_happens_without_logging_in(anon):
    for path in ("/templates", "/printers", "/datasources", "/runs", "/queries"):
        assert anon.get(path).status_code == 401, f"GET {path} answered"
    for path in ("/runs", "/printers/scan", "/templates/import"):
        assert anon.post(path, json={}).status_code == 401, f"POST {path} answered"
    assert anon.get("/datasources/warehouse/reveal").status_code == 401


def test_every_route_says_who_may_call_it(anon):
    """The guard against the next endpoint being added without one."""
    open_paths = {
        "/auth/login", "/auth/logout", "/auth/me", "/studio/{screen}",
        "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    }
    unguarded = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if path in open_paths or path.startswith("/studio/static"):
            continue
        # route-level Depends(...) carry .dependency; the resolved tree of
        # Dependant objects carries .call
        names = {
            getattr(d, "dependency", None).__name__
            for d in getattr(route, "dependencies", [])
            if getattr(d, "dependency", None) is not None
        }
        names |= {
            d.call.__name__
            for d in getattr(getattr(route, "dependant", None), "dependencies", [])
            if getattr(d, "call", None) is not None
        }
        if not names & {"current_user", "admin", "agent_token"}:
            unguarded.append(f"{sorted(getattr(route, 'methods', []))} {path}")

    assert not unguarded, f"routes with no one guarding them: {unguarded}"


# ------------------------------------------------------------------- roles

def test_an_operator_can_do_the_job_they_are_there_for(operator):
    check = operator.post("/runs/check", json={"template_id": "carton", "params": PARAMS})
    assert check.status_code == 200

    run = operator.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                       "params": PARAMS})
    assert run.status_code == 202
    assert operator.get(f"/runs/{run.json()['id']}").status_code == 200
    assert operator.post(f"/runs/{run.json()['id']}/cancel").status_code == 202
    assert operator.get("/templates").status_code == 200
    assert operator.get("/printers").status_code == 200


def test_an_operator_cannot_change_how_the_label_looks(operator):
    from tests.conftest import template_body

    assert operator.put("/templates/carton", json=template_body("blank")).status_code == 403
    assert operator.post("/templates/carton/publish").status_code == 403
    assert operator.delete("/templates/carton").status_code == 403


def test_an_operator_cannot_reach_the_database_credentials(operator):
    assert operator.get("/datasources").status_code == 403
    assert operator.get("/datasources/warehouse/reveal").status_code == 403
    assert operator.put("/datasources/warehouse", json={"name": "x", "url": "sqlite://"}).status_code == 403


def test_an_operator_is_not_shown_the_sql_behind_their_question(operator):
    """They need the parameters to answer. The query itself is not their business."""
    body = operator.get("/queries/cartons").json()

    assert [p["name"] for p in body["parameters"]] == ["despatch_date"]
    assert "sql" not in body


def test_an_operator_cannot_rewire_the_printers(operator):
    assert operator.put("/printers/dock", json={"name": "x", "transport": {"kind": "memory"}}).status_code == 403
    assert operator.post("/printers/scan", json={"network": "127.0.0.1/32"}).status_code == 403
    assert operator.delete("/printers/dock").status_code == 403


def test_an_administrator_sees_the_sql(seeded):
    assert "sql" in seeded.get("/queries/cartons").json()


# --------------------------------------------------------------- the record

def test_the_audit_log_says_who_did_it(seeded):
    from db.models import AuditLog

    seeded.post("/templates/carton/publish")

    with dbsession.SessionLocal() as s:
        entry = s.scalars(select(AuditLog).where(AuditLog.action == "publish")).all()[-1]
    assert entry.actor == "admin"


# ------------------------------------------------------------- first run

def test_the_first_administrator_can_be_made_from_the_environment(db, monkeypatch):
    monkeypatch.setenv("PLATEN_ADMIN_USERNAME", "boss")
    monkeypatch.setenv("PLATEN_ADMIN_PASSWORD", "a-long-enough-password")

    auth.bootstrap()

    with dbsession.SessionLocal() as s:
        assert s.get(AppUser, "boss").role == "admin"


def test_bootstrapping_twice_does_not_reset_anyone(db, monkeypatch):
    monkeypatch.setenv("PLATEN_ADMIN_USERNAME", "boss")
    monkeypatch.setenv("PLATEN_ADMIN_PASSWORD", "a-long-enough-password")
    auth.bootstrap()
    with dbsession.SessionLocal() as s:
        first = s.get(AppUser, "boss").password

    monkeypatch.setenv("PLATEN_ADMIN_PASSWORD", "something-different")
    auth.bootstrap()

    with dbsession.SessionLocal() as s:
        assert s.get(AppUser, "boss").password == first


def test_a_password_too_short_to_be_worth_anything_is_refused(db):
    with pytest.raises(ValueError) as exc:
        auth.create_user(dbsession.SessionLocal(), "dave", "short", role="operator")

    assert "characters" in str(exc.value)


def test_the_login_screen_is_served(anon):
    r = anon.get("/studio/login")
    assert r.status_code == 200
    assert "Sign in" in r.text
