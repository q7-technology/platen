"""The local agent: a printer on somebody's desk, over USB.

The agent asks Platen for work rather than Platen reaching into the office
network, so nothing has to be open inbound to a workstation. A label is only
counted as printed once the agent says it came out.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select

from app import printers
from db import session as dbsession
from db.models import AgentJob, ApiToken

PARAMS = {"despatch_date": "2026-09-18"}


@pytest.fixture
def agent(client) -> dict:
    """An agent registered by an administrator, with its one-time token."""
    made = client.put("/agents/wks-office-02", json={"name": "Office workstation"})
    assert made.status_code == 200, made.text
    return made.json()


def _as_agent(client, token: str):
    client.headers["authorization"] = f"Bearer {token}"
    return client


def _poll(client, token: str, agent_id: str = "wks-office-02"):
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    c.headers["authorization"] = f"Bearer {token}"
    return c.post(f"/agents/{agent_id}/poll", json={"devices": ["ZDesigner ZD621"]})


# ------------------------------------------------------------------- tokens

def test_the_token_is_shown_once_and_never_again(client, agent):
    assert agent["token"].startswith("plt_")

    again = client.get("/agents/wks-office-02").json()

    assert "token" not in again
    with dbsession.SessionLocal() as s:
        stored = s.scalars(select(ApiToken)).all()[0]
    assert agent["token"] not in stored.id


def test_an_agent_token_cannot_wander_into_the_rest_of_platen(anon, users, agent):
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    c.headers["authorization"] = f"Bearer {agent['token']}"

    assert c.get("/templates").status_code == 403
    assert c.get("/datasources").status_code == 403
    assert c.post("/runs", json={}).status_code == 403


def test_a_token_for_one_agent_is_no_use_on_another(client, agent):
    client.put("/agents/other-desk", json={"name": "Someone else"})

    assert _poll(client, agent["token"], "other-desk").status_code == 403


def test_rotating_the_token_shuts_the_old_one_out(client, agent):
    fresh = client.post("/agents/wks-office-02/token").json()["token"]

    assert fresh != agent["token"]
    assert _poll(client, fresh).status_code == 200
    assert _poll(client, agent["token"]).status_code == 401


def test_nonsense_in_the_header_is_refused(anon, users, agent):
    from fastapi.testclient import TestClient

    from app.main import app
    c = TestClient(app)
    c.headers["authorization"] = "Bearer plt_not_a_real_token"

    assert c.post("/agents/wks-office-02/poll", json={}).status_code == 401


# -------------------------------------------------------------------- jobs

def test_polling_an_idle_agent_finds_nothing(client, agent):
    body = _poll(client, agent["token"]).json()

    assert body["jobs"] == []


def test_an_agent_that_has_never_polled_is_not_online(client, agent):
    assert client.get("/agents/wks-office-02").json()["online"] is False


def test_polling_marks_it_online(client, agent):
    _poll(client, agent["token"])

    assert client.get("/agents/wks-office-02").json()["online"] is True


def _printer_on_the_agent(client) -> None:
    assert client.put("/printers/desk", json={
        "name": "Office", "model": "ZD621",
        "transport": {"kind": "agent", "agent_id": "wks-office-02",
                      "device": "ZDesigner ZD621"}}).status_code == 200


def test_sending_waits_until_the_agent_says_it_came_out(client, agent):
    """A socket printer has accepted the bytes when send returns. An agent
    printer should mean the same thing, or "printed" is a lie."""
    _printer_on_the_agent(client)
    token = agent["token"]
    done = []

    def the_agent() -> None:
        for _ in range(100):
            body = _poll(client, token).json()
            if body["jobs"]:
                job = body["jobs"][0]
                done.append(job)
                from fastapi.testclient import TestClient

                from app.main import app
                c = TestClient(app)
                c.headers["authorization"] = f"Bearer {token}"
                c.post(f"/agents/wks-office-02/jobs/{job['id']}/done", json={})
                return
            time.sleep(0.05)

    thread = threading.Thread(target=the_agent, daemon=True)
    thread.start()
    with dbsession.SessionLocal() as s:
        printers.load(s, "desk").transport.send(b"^XAhello^XZ")
    thread.join(timeout=10)

    assert len(done) == 1
    assert done[0]["zpl"] == "^XAhello^XZ"
    assert done[0]["device"] == "ZDesigner ZD621"


def test_a_job_is_only_handed_out_once(client, agent):
    _printer_on_the_agent(client)
    with dbsession.SessionLocal() as s:
        s.add(AgentJob(id="j1", agent_id="wks-office-02", device="d", zpl="^XA^XZ"))
        s.commit()

    first = _poll(client, agent["token"]).json()["jobs"]
    second = _poll(client, agent["token"]).json()["jobs"]

    assert [j["id"] for j in first] == ["j1"]
    assert second == []


def test_an_agent_that_cannot_print_says_why(client, agent):
    _printer_on_the_agent(client)
    token = agent["token"]

    def the_agent() -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        c = TestClient(app)
        c.headers["authorization"] = f"Bearer {token}"
        for _ in range(100):
            body = c.post("/agents/wks-office-02/poll", json={}).json()
            if body["jobs"]:
                c.post(f"/agents/wks-office-02/jobs/{body['jobs'][0]['id']}/done",
                       json={"error": "the printer is out of labels"})
                return
            time.sleep(0.05)

    threading.Thread(target=the_agent, daemon=True).start()
    with dbsession.SessionLocal() as s, pytest.raises(Exception) as exc:
        printers.load(s, "desk").transport.send(b"^XA^XZ")

    assert "out of labels" in str(exc.value)


def test_an_agent_that_never_answers_gives_up_rather_than_hanging(client, agent, monkeypatch):
    monkeypatch.setattr(printers, "AGENT_TIMEOUT", 0.4)
    _printer_on_the_agent(client)

    with dbsession.SessionLocal() as s, pytest.raises(Exception) as exc:
        printers.load(s, "desk").transport.send(b"^XA^XZ")

    assert "wks-office-02" in str(exc.value)


def test_an_agent_printer_is_offline_until_the_agent_checks_in(client, agent):
    _printer_on_the_agent(client)

    assert client.get("/printers/desk", params={"probe": "true"}).json()["online"] is False
    _poll(client, agent["token"])
    assert client.get("/printers/desk", params={"probe": "true"}).json()["online"] is True


def test_the_agent_screen_lists_them(client, agent):
    rows = client.get("/agents").json()

    assert [a["id"] for a in rows] == ["wks-office-02"]
    assert rows[0]["name"] == "Office workstation"
    assert "token" not in rows[0]
