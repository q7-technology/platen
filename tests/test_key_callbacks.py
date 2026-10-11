"""A print job is reported back to whoever sent it: the callback set on the
key that sent the job, or WMS_URL for a key without one."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs
from app.main import app
from db import session as dbsession
from db.models import ApiToken, PrintRun
from tests.test_wms_intake import _job, _settle, wms  # noqa: F401  (fixture)

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
    r = client.patch(f"/keys/{made['id']}", json={"callback_url": "http://tms.test:8022/",
                                                  "callback_key": "***"})
    assert r.status_code == 200
    row = _stored("simple-tms")
    assert row.callback_url == "http://tms.test:8022"
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


def test_a_job_is_reported_to_the_key_that_sent_it(seeded, wms, caplog):  # noqa: F811
    job = _job()
    run_id = _as(_make(seeded, **TMS)).post("/intake/jobs", json=job).json()["id"]
    _settle(run_id, "done")
    with caplog.at_level(logging.DEBUG):
        jobs.report_to_wms(run_id)
    call = wms.calls[-1]
    assert call["url"] == f"http://tms.test:8022/v1/print-jobs/{job['job_id']}/status"
    assert call["json"] == {"status": "printed"}
    assert call["headers"]["Authorization"] == "Bearer tms_k.callback-secret"
    assert "callback-secret" not in caplog.text


def test_a_key_without_a_callback_still_reports_to_wms_url(seeded, wms):  # noqa: F811
    job = _job()
    run_id = _as(_make(seeded)).post("/intake/wms", json=job).json()["id"]
    _settle(run_id, "done")
    jobs.report_to_wms(run_id)
    assert wms.calls[-1]["url"] == f"http://wms.test/v1/print-jobs/{job['job_id']}/status"
    assert wms.calls[-1]["headers"]["Authorization"] == "Bearer wms_live_secret"


def test_a_callback_without_a_credential_is_sent_bare_and_says_so(seeded, wms, caplog):  # noqa: F811
    made = _make(seeded, callback_url="http://tms.test:8022")
    run_id = _as(made).post("/intake/jobs", json=_job()).json()["id"]
    _settle(run_id, "done")
    with caplog.at_level(logging.WARNING):
        jobs.report_to_wms(run_id)
    assert "Authorization" not in wms.calls[-1]["headers"]
    assert "simple-tms" in caplog.text and "callback key" in caplog.text


def test_a_job_from_a_revoked_key_is_reported_to_nobody(seeded, wms, caplog):  # noqa: F811
    made = _make(seeded, **TMS)
    run_id = _as(made).post("/intake/jobs", json=_job()).json()["id"]
    assert seeded.delete(f"/keys/{made['id']}").status_code == 204
    _settle(run_id, "done")
    with caplog.at_level(logging.WARNING):
        jobs.report_to_wms(run_id)
    assert wms.calls == []
    assert "revoked" in caplog.text and run_id in caplog.text


def test_the_keys_screen_asks_for_a_callback(client):
    r = client.get("/studio/people")
    assert 'id="k-callback-url"' in r.text and 'id="k-callback-key"' in r.text


def test_a_credential_left_out_keeps_the_stored_one(client):
    made = _make(client, **TMS)
    r = client.patch(f"/keys/{made['id']}", json={"callback_url": "http://tms.test:8022"})
    assert r.status_code == 200, r.text
    assert _stored("simple-tms").callback_key == "tms_k.callback-secret"


def test_a_credential_sent_empty_or_null_is_cleared(client):
    made = _make(client, **TMS)
    for empty in ("", None):
        client.patch(f"/keys/{made['id']}", json=TMS)
        r = client.patch(f"/keys/{made['id']}", json={"callback_url": "http://tms.test:8022",
                                                      "callback_key": empty})
        assert r.status_code == 200, r.text
        assert _stored("simple-tms").callback_key is None


def test_a_new_address_needs_the_credential_typed_again(client):
    made = _make(client, **TMS)
    for kept in ({}, {"callback_key": "***"}):
        r = client.patch(f"/keys/{made['id']}",
                         json={"callback_url": "https://elsewhere.example.com", **kept})
        assert r.status_code == 422
        assert "callback_key" in r.json()["detail"]
        assert "again" in r.json()["detail"]
    row = _stored("simple-tms")
    assert row.callback_url == "http://tms.test:8022"
    assert row.callback_key == "tms_k.callback-secret"
    r = client.patch(f"/keys/{made['id']}", json={"callback_url": "https://elsewhere.example.com",
                                                  "callback_key": "tms_k.new"})
    assert r.status_code == 200
    assert _stored("simple-tms").callback_key == "tms_k.new"


def test_a_new_address_for_a_key_without_a_credential_is_fine(client):
    made = _make(client, callback_url="http://tms.test:8022")
    r = client.patch(f"/keys/{made['id']}", json={"callback_url": "https://elsewhere.example.com"})
    assert r.status_code == 200
    assert _stored("simple-tms").callback_url == "https://elsewhere.example.com"


def test_a_callback_address_carries_no_query_or_fragment(client):
    for bad in ("http://tms.test/?token=secret", "http://tms.test/#secret", "http://tms.test?"):
        r = client.post("/keys", json={"name": "x", "callback_url": bad})
        assert r.status_code == 422, bad
        assert "callback_url" in r.json()["detail"] and "secret" not in r.json()["detail"]
    made = _make(client, **TMS)
    r = client.patch(f"/keys/{made['id']}", json={"callback_url": "http://tms.test:8022?t=1",
                                                  "callback_key": "x"})
    assert r.status_code == 422 and "callback_url" in r.json()["detail"]
    assert client.get("/keys").json()[0]["callback_url"] == "http://tms.test:8022"
