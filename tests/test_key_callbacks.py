"""A print job is reported back to whoever sent it: the callback set on the
key that sent the job, or WMS_URL for a key without one."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from db import session as dbsession
from db.models import ApiToken, PrintRun
from tests.test_wms_intake import _job

TMS = {"callback_url": "http://tms.test:8022/", "callback_key": "tms_k.callback-secret"}


def _make(admin: TestClient, **body) -> dict:
    r = admin.post("/keys", json={"name": "simple-tms", "role": "operator", **body})
    assert r.status_code == 201, r.text
    return r.json()


def _as(made: dict) -> TestClient:
    return TestClient(app, headers={"authorization": f"Bearer {made['token']}"})


def _stored(name: str) -> ApiToken:
    with dbsession.SessionLocal() as s:
        return s.scalars(select(ApiToken).where(ApiToken.name == name)).one()


def test_a_key_carries_a_callback_and_never_shows_its_credential(client):
    made = _make(client, **TMS)
    assert made["callback_url"] == "http://tms.test:8022"
    assert made["callback_key"] == "***"
    listed = client.get("/keys")
    assert listed.json()[0]["callback_url"] == "http://tms.test:8022"
    assert listed.json()[0]["callback_key"] == "***"
    assert "callback-secret" not in listed.text and "callback-secret" not in str(made)
    assert _stored("simple-tms").callback_key == "tms_k.callback-secret"


def test_a_key_without_a_callback_says_so(client):
    made = _make(client)
    assert made["callback_url"] is None and made["callback_key"] is None
    assert client.get("/keys").json()[0]["callback_url"] is None


def test_a_callback_credential_needs_an_address(client):
    r = client.post("/keys", json={"name": "x", "callback_key": "secret"})
    assert r.status_code == 422 and "callback_url" in r.json()["detail"]
    assert client.get("/keys").json() == []


def test_a_callback_address_is_http_and_carries_no_credential(client):
    for bad in ("ftp://tms.test", "tms.test", "http://", "http://u:secret@tms.test"):
        r = client.post("/keys", json={"name": "x", "callback_url": bad})
        assert r.status_code == 422, bad
        assert "callback_url" in r.json()["detail"]
        assert "secret" not in r.json()["detail"]
    assert client.get("/keys").json() == []


def test_an_existing_key_is_given_a_callback(client):
    made = _make(client)
    r = client.patch(f"/keys/{made['id']}", json=TMS)
    assert r.status_code == 200, r.text
    assert r.json()["callback_url"] == "http://tms.test:8022"
    assert r.json()["callback_key"] == "***"
    assert _stored("simple-tms").callback_key == "tms_k.callback-secret"


def test_a_credential_saved_back_masked_keeps_the_stored_one(client):
    made = _make(client, **TMS)
    r = client.patch(f"/keys/{made['id']}", json={"callback_url": "https://tms.example.com",
                                                  "callback_key": "***"})
    assert r.status_code == 200
    row = _stored("simple-tms")
    assert row.callback_url == "https://tms.example.com"
    assert row.callback_key == "tms_k.callback-secret"


def test_no_address_clears_the_callback(client):
    made = _make(client, **TMS)
    assert client.patch(f"/keys/{made['id']}", json={"callback_url": None}).status_code == 200
    row = _stored("simple-tms")
    assert row.callback_url is None and row.callback_key is None


def test_only_an_administrator_sets_a_callback(client, operator):
    made = _make(client)
    assert operator.patch(f"/keys/{made['id']}", json=TMS).status_code == 403
    assert client.patch("/keys/nosuchkey", json=TMS).status_code == 404


def test_the_run_remembers_which_key_sent_it(seeded):
    made = _make(seeded, **TMS)
    run_id = _as(made).post("/intake/jobs", json=_job()).json()["id"]
    with dbsession.SessionLocal() as s:
        assert s.get(PrintRun, run_id).sent_by_key == _stored("simple-tms").id


def test_the_neutral_intake_and_the_wms_one_are_the_same_door(seeded, queue):
    c, job = _as(_make(seeded)), _job()
    first = c.post("/intake/jobs", json=job)
    assert first.status_code == 202 and first.json()["duplicate"] is False
    again = c.post("/intake/wms", json=job)
    assert again.json()["id"] == first.json()["id"] and again.json()["duplicate"] is True
    assert queue.count == 1
