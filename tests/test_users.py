"""Managing the people and keys that can reach Platen.

An auth system you can only add to is half a system: the half that matters on
a bad day is taking access away.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from db import session as dbsession
from db.models import ApiToken, AppUser, UserSession


def _signed_in(who: str, password: str) -> TestClient:
    c = TestClient(app)
    assert c.post("/auth/login", json={"username": who, "password": password}).status_code == 200
    return c


def _sessions(username: str) -> int:
    with dbsession.SessionLocal() as s:
        return len(s.scalars(select(UserSession).where(UserSession.username == username)).all())


# -------------------------------------------------------------------- people

def test_an_administrator_can_see_who_has_access(client, users):
    rows = {u["username"]: u for u in client.get("/users").json()}

    assert set(rows) == {"admin", "dave"}
    assert rows["dave"]["role"] == "operator"
    assert rows["dave"]["name"] == "Dave"
    assert "password" not in rows["dave"]


def test_a_new_person_can_be_made_and_can_sign_in(client, users):
    r = client.put("/users/kelly", json={
        "name": "Kelly Tran", "role": "operator", "password": "kelly-on-nights"})

    assert r.status_code == 200
    assert _signed_in("kelly", "kelly-on-nights").get("/auth/me").json()["role"] == "operator"


def test_making_someone_without_a_password_is_refused(client, users):
    r = client.put("/users/kelly", json={"name": "Kelly", "role": "operator"})

    assert r.status_code == 422
    assert "password" in r.json()["detail"]


def test_resetting_a_password_ends_the_sessions_that_had_the_old_one(client, users):
    dave = _signed_in("dave", "dave-password")
    assert dave.get("/auth/me").status_code == 200

    client.post("/users/dave/password", json={"password": "a-brand-new-password"})

    assert dave.get("/auth/me").status_code == 401
    assert _sessions("dave") == 0
    assert _signed_in("dave", "a-brand-new-password").get("/auth/me").status_code == 200


def test_switching_someone_off_stops_them_mid_shift(client, users):
    dave = _signed_in("dave", "dave-password")

    client.put("/users/dave", json={"name": "Dave", "role": "operator", "disabled": True})

    assert dave.get("/auth/me").status_code == 401
    assert _sessions("dave") == 0
    r = TestClient(app).post("/auth/login", json={"username": "dave", "password": "dave-password"})
    assert r.status_code == 401


def test_signing_someone_out_everywhere_without_changing_their_password(client, users):
    dave = _signed_in("dave", "dave-password")

    client.post("/users/dave/sessions/end")

    assert dave.get("/auth/me").status_code == 401
    assert _signed_in("dave", "dave-password").get("/auth/me").status_code == 200


def test_you_cannot_lock_yourself_out(client, users):
    demoted = client.put("/users/admin", json={"name": "L. Lauton", "role": "operator"})
    switched_off = client.put("/users/admin", json={"name": "L. Lauton", "role": "admin",
                                                    "disabled": True})

    assert demoted.status_code == 409
    assert switched_off.status_code == 409
    assert client.get("/auth/me").json()["role"] == "admin"


def test_you_cannot_delete_yourself_even_as_an_administrator(client, users):
    r = client.delete("/users/admin")

    assert r.status_code == 409
    assert "yourself" in r.json()["detail"]


def test_a_key_cannot_delete_the_last_administrator(client, users):
    """A key with admin rights is not a person, so the self-delete rule does
    not catch it. Something has to."""
    made = client.post("/keys", json={"name": "ci", "role": "admin"}).json()
    c = TestClient(app)
    c.headers["authorization"] = f"Bearer {made['token']}"
    assert c.delete("/users/dave").status_code == 204

    r = c.delete("/users/admin")

    assert r.status_code == 409
    assert "administrator" in r.json()["detail"]


def test_deleting_someone_takes_their_sessions_with_them(client, users):
    dave = _signed_in("dave", "dave-password")

    assert client.delete("/users/dave").status_code == 204

    assert dave.get("/auth/me").status_code == 401
    assert _sessions("dave") == 0
    with dbsession.SessionLocal() as s:
        assert s.get(AppUser, "dave") is None


def test_an_operator_has_no_business_here(operator):
    assert operator.get("/users").status_code == 403
    assert operator.put("/users/kelly", json={"password": "x" * 14}).status_code == 403
    assert operator.post("/users/admin/password", json={"password": "x" * 14}).status_code == 403
    assert operator.get("/keys").status_code == 403


# ---------------------------------------------------------------------- keys

def test_a_key_for_a_script_is_made_and_shown_once(client, users):
    made = client.post("/keys", json={"name": "nightly relabel", "role": "operator"}).json()

    assert made["token"].startswith("plt_")
    listed = client.get("/keys").json()
    assert [k["name"] for k in listed] == ["nightly relabel"]
    assert "token" not in listed[0]


def test_that_key_can_do_the_job_and_no_more(seeded, users):
    made = seeded.post("/keys", json={"name": "nightly", "role": "operator"}).json()
    c = TestClient(app)
    c.headers["authorization"] = f"Bearer {made['token']}"

    assert c.get("/templates").status_code == 200
    assert c.post("/runs/check", json={"template_id": "carton",
                                       "params": {"despatch_date": "2026-09-18"}}).status_code == 200
    assert c.get("/datasources").status_code == 403


def test_revoking_a_key_stops_it(client, users):
    made = client.post("/keys", json={"name": "nightly", "role": "operator"}).json()
    c = TestClient(app)
    c.headers["authorization"] = f"Bearer {made['token']}"
    assert c.get("/templates").status_code == 200

    assert client.delete(f"/keys/{made['id']}").status_code == 204

    assert c.get("/templates").status_code == 401


def test_an_agent_key_is_not_made_here(client, users):
    """An agent's key comes with the agent, so it carries an agent id. One
    made loose here would answer for every agent at once."""
    r = client.post("/keys", json={"name": "sneaky", "role": "agent"})

    assert r.status_code == 422
    with dbsession.SessionLocal() as s:
        assert not [k for k in s.scalars(select(ApiToken)) if k.name == "sneaky"]


def test_a_key_says_when_it_was_last_used(seeded, users):
    made = seeded.post("/keys", json={"name": "nightly", "role": "operator"}).json()
    c = TestClient(app)
    c.headers["authorization"] = f"Bearer {made['token']}"
    c.get("/templates")

    listed = seeded.get("/keys").json()[0]

    assert listed["last_used_at"] is not None


def test_the_people_screen_is_served(client):
    r = client.get("/studio/people")
    assert r.status_code == 200
    assert "People" in r.text
