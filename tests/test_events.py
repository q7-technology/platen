"""Live updates: the worker and the API say what moved, and each viewer only
hears about printers at their own sites."""

from __future__ import annotations

import json

from app import events, jobs
from tests.test_health import heard  # noqa: F401  (a fixture)

PARAMS = {"despatch_date": "2026-09-18"}


def _run(client) -> str:
    return client.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                      "params": PARAMS}).json()["id"]


def test_a_run_says_every_step_it_takes(seeded, heard):  # noqa: F811
    run_id = _run(seeded)
    jobs.print_run(run_id)
    said = [e for e in heard() if e["kind"] == "run" and e["id"] == run_id]
    assert said[0]["status"] == "queued"
    assert said[1]["status"] == "printing"
    assert [e["printed"] for e in said if e["status"] == "printing"][1:] == list(range(1, 13))
    assert said[-1]["status"] == "done" and said[-1]["printed"] == 12
    assert all(e["printer_id"] == "dock" for e in said)


def test_holding_the_queue_is_announced(seeded, heard):  # noqa: F811
    seeded.post("/queue/pause")
    seeded.post("/queue/resume")
    assert [e["paused"] for e in heard() if e["kind"] == "queue"] == [True, False]


def test_a_dead_redis_never_stops_a_print(seeded, transport, monkeypatch):
    def broken():
        raise ConnectionError("redis has gone")
    run_id = _run(seeded)
    monkeypatch.setattr(jobs, "connection", broken)
    jobs.print_run(run_id)
    assert len(transport.sent) == 12


def test_who_hears_what():
    about_a = {"kind": "run", "printer_id": "a"}
    about_nobody = {"kind": "queue", "paused": True}
    assert events.shows(about_a, None)
    assert events.shows(about_a, {"a"})
    assert not events.shows(about_a, {"b"})
    assert events.shows(about_nobody, {"b"})


def test_the_stream_only_carries_your_printers(queue):
    sent = 0

    def stop() -> bool:
        return sent >= 3

    stream = events.stream(lambda: {"bal"}, stop=stop, heartbeat=3600)
    assert next(stream) == "retry: 3000\n\n"          # subscribed from here on
    events.publish("run", id="JOB-1", printer_id="gee", status="printing")
    events.publish("run", id="JOB-2", printer_id="bal", status="printing")
    events.publish("queue", paused=True)

    got = []
    for chunk in stream:
        got.append(chunk)
        sent += 1
        if len(got) == 2:
            break
    stream.close()
    kinds = [c.split("\n")[0] for c in got]
    assert kinds == ["event: run", "event: queue"]
    assert json.loads(got[0].split("data: ", 1)[1])["id"] == "JOB-2"


def test_a_quiet_stream_still_says_it_is_there(queue):
    stream = events.stream(lambda: None, heartbeat=0)
    next(stream)
    assert next(stream) == ": still here\n\n"
    stream.close()


def test_nobody_listens_without_signing_in(anon):
    assert anon.get("/events").status_code == 401
