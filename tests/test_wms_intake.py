"""Print jobs sent by Simple WMS: a template name, a printer, and the data."""

from __future__ import annotations

import dataclasses
import logging
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app import jobs, main
from app import settings as settings_module
from app.main import _wms_records, app
from db import session as dbsession
from db.models import PrintRun, RunLabel, Template
from tests.conftest import template_body
from tests.test_pages import office, orders  # noqa: F401  (fixtures)
from tests.test_sites import two_sites  # noqa: F401  (fixture)

CARTON = {"consignee_name": "Acme Auto Parts", "consignment_no": "7MB9042199", "logo": None}


def _job(**over) -> dict:
    return {"job_id": str(uuid.uuid4()), "template": "carton", "version": "v3",
            "printer": "dock", "copies": 1,
            "reference": {"type": "delivery", "ref": "0080012345"}, "data": CARTON, **over}


def _key(admin: TestClient, **body) -> TestClient:
    token = admin.post("/keys", json={"name": "wms", "role": "operator", **body}).json()["token"]
    return TestClient(app, headers={"authorization": f"Bearer {token}"})


def test_a_wms_job_prints_from_its_own_data(seeded, queue):
    r = _key(seeded).post("/intake/wms", json=_job())
    assert r.status_code == 202, r.text
    assert r.json()["labels"] == 1 and r.json()["duplicate"] is False
    assert queue.count == 1
    with dbsession.SessionLocal() as s:
        label = s.scalars(select(RunLabel)).one()
        assert "Acme Auto Parts" in label.zpl and "7MB9042199" in label.zpl


def test_the_printer_is_found_by_its_name_as_well(seeded):
    assert _key(seeded).post("/intake/wms", json=_job(printer=" dock 1 ")).json()["printer_id"] == "dock"


def test_a_resent_job_prints_once(seeded, queue):
    c, job = _key(seeded), _job()
    first = c.post("/intake/wms", json=job).json()
    again = c.post("/intake/wms", json=job)
    assert again.status_code == 202
    assert again.json()["id"] == first["id"] and again.json()["duplicate"] is True
    assert queue.count == 1


def test_a_job_that_never_reached_the_queue_gets_another_go(seeded, queue, monkeypatch):
    c, job = _key(seeded), _job()
    real = jobs.enqueue

    def down(run_id):
        raise ConnectionError("redis")
    monkeypatch.setattr(jobs, "enqueue", down)
    assert c.post("/intake/wms", json=job).status_code == 503
    assert c.post("/intake/wms", json=job).status_code == 503     # still down: still failed
    monkeypatch.setattr(jobs, "enqueue", real)
    again = c.post("/intake/wms", json=job)
    assert again.status_code == 202 and queue.count == 1
    assert seeded.get(f"/runs/{again.json()['id']}").json()["status"] == "queued"


def test_what_platen_does_not_have_is_named(seeded):
    c = _key(seeded)
    r = c.post("/intake/wms", json=_job(template="pallet-label"))
    assert r.status_code == 422 and "pallet-label" in r.json()["detail"]
    r = c.post("/intake/wms", json=_job(printer="Packing bench 9"))
    assert r.status_code == 422 and "Packing bench 9" in r.json()["detail"]
    r = c.post("/intake/wms", json=_job(data={"consignee_name": "Acme"}))
    assert r.status_code == 422 and "consignment_no" in r.json()["detail"]


def test_a_key_kept_to_one_site_prints_only_there(two_sites):  # noqa: F811
    c = _key(two_sites, sites=["bal"])
    assert c.post("/intake/wms", json=_job(printer="gee-despatch")).status_code == 403
    assert c.post("/intake/wms", json=_job(printer="bal-despatch")).status_code == 202


def test_a_resent_job_at_another_site_says_nothing_about_it(two_sites):  # noqa: F811
    job = _job(printer="gee-despatch")
    assert _key(two_sites).post("/intake/wms", json=job).status_code == 202
    assert _key(two_sites, name="wms-bal", sites=["bal"]).post(
        "/intake/wms", json=job).status_code == 403


def test_nobody_signed_in_is_refused(anon):
    assert anon.post("/intake/wms", json=_job()).status_code == 401


def test_a_page_gets_a_row_per_line_with_the_header_beside_it():
    data = {"order_no": "SO-9", "customer": "IGA", "lines": [
        {"sku": "A", "qty": 1}, {"sku": "B", "qty": 2}]}
    assert _wms_records("page", data) == [
        {"order_no": "SO-9", "customer": "IGA", "sku": "A", "qty": 1},
        {"order_no": "SO-9", "customer": "IGA", "sku": "B", "qty": 2}]
    assert _wms_records("label", data) == [data]
    assert _wms_records("page", {"order_no": "SO-9"}) == [{"order_no": "SO-9"}]


def test_a_packing_slip_from_the_wms_is_one_document(office):  # noqa: F811
    r = _key(office).post("/intake/wms", json=_job(
        template="slip", printer="laser",
        data={"order_no": "SO-9", "customer": "IGA", "lines": [
            {"sku": "A", "descr": "Wipers", "qty": 1}, {"sku": "B", "descr": "Fluid", "qty": 2}]}))
    assert r.status_code == 202, r.text
    assert r.json()["labels"] == 1


def _queued(queue) -> list[str]:
    """What is waiting on the reports queue: the same connection as the print
    queue, but its own queue, so a report never holds up a label."""
    return [j.func_name for j in jobs.reports_queue().jobs]


def _printed_by_platen(seeded, **job) -> str:
    return _key(seeded).post("/intake/wms", json=_job(**job)).json()["id"]


def test_a_finished_run_queues_the_report(seeded, queue):
    run_id = _printed_by_platen(seeded)
    jobs.print_run(run_id)
    assert _queued(queue).count("app.jobs.report_to_wms") == 1


def test_a_run_from_the_studio_reports_to_nobody(seeded, queue):
    run_id = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                        "params": {"despatch_date": "2026-09-18"}}).json()["id"]
    jobs.print_run(run_id)
    assert "app.jobs.report_to_wms" not in _queued(queue)


def test_a_failed_run_queues_the_report(seeded, queue, transport):
    run_id = _printed_by_platen(seeded)

    def boom(n):
        raise OSError("stopped answering")
    transport.after_send = boom
    with pytest.raises(OSError):
        jobs.print_run(run_id)
    assert "app.jobs.report_to_wms" in _queued(queue)


def test_a_cancelled_run_queues_the_report(seeded, queue):
    run_id = _printed_by_platen(seeded)
    seeded.post(f"/runs/{run_id}/cancel")
    jobs.print_run(run_id)
    assert seeded.get(f"/runs/{run_id}").json()["status"] == "cancelled"
    assert "app.jobs.report_to_wms" in _queued(queue)


class Wms:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.status = 200

    def post(self, url, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if isinstance(self.status, Exception):
            raise self.status
        return type("R", (), {"status_code": self.status})()


@pytest.fixture
def wms(monkeypatch) -> Wms:
    w = Wms()
    monkeypatch.setattr(jobs.httpx, "post", w.post)
    monkeypatch.setattr(settings_module, "settings", dataclasses.replace(
        settings_module.settings, wms_url="http://wms.test/", wms_key="wms_live_secret"))
    return w


def _settle(run_id: str, status: str, error: str | None = None) -> None:
    with dbsession.SessionLocal() as s:
        run = s.get(PrintRun, run_id)
        run.status, run.error = status, error
        s.commit()


@pytest.mark.parametrize("status,error,sent", [
    ("done", None, {"status": "printed"}),
    ("failed", "OSError: stopped answering", {"status": "failed",
                                               "message": "OSError: stopped answering"}),
    ("cancelled", None, {"status": "failed", "message": "cancelled in Platen"}),
])
def test_the_wms_hears_how_it_went(seeded, wms, caplog, status, error, sent):
    job = _job()
    run_id = _key(seeded).post("/intake/wms", json=job).json()["id"]
    _settle(run_id, status, error)
    with caplog.at_level(logging.DEBUG):
        jobs.report_to_wms(run_id)
    call = wms.calls[-1]
    assert call["url"] == f"http://wms.test/v1/print-jobs/{job['job_id']}/status"
    assert call["json"] == sent
    assert call["headers"]["Authorization"] == "Bearer wms_live_secret"
    assert "wms_live_secret" not in caplog.text


def test_a_wms_that_refuses_is_tried_again(seeded, wms):
    run_id = _printed_by_platen(seeded)
    _settle(run_id, "done")
    wms.status = 503
    with pytest.raises(RuntimeError, match="503"):
        jobs.report_to_wms(run_id)


def test_a_run_printing_again_says_nothing_yet(seeded, wms):
    run_id = _printed_by_platen(seeded)
    _settle(run_id, "printing")
    jobs.report_to_wms(run_id)
    assert wms.calls == []


def test_without_a_wms_url_nothing_is_sent(seeded, monkeypatch):
    sent = []
    monkeypatch.setattr(jobs.httpx, "post", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(settings_module, "settings",
                        dataclasses.replace(settings_module.settings, wms_url=""))
    run_id = _printed_by_platen(seeded)
    _settle(run_id, "done")
    jobs.report_to_wms(run_id)
    assert sent == []


def test_the_report_is_queued_apart_from_the_labels(seeded, queue):
    run_id = _printed_by_platen(seeded)
    jobs.print_run(run_id)
    assert "app.jobs.report_to_wms" not in [j.func_name for j in queue.jobs]
    assert jobs.reports_queue().name == "platen-reports"


def test_the_report_gives_up_on_a_slow_connect_quickly(seeded, wms):
    run_id = _printed_by_platen(seeded)
    _settle(run_id, "done")
    jobs.report_to_wms(run_id)
    timeout = wms.calls[-1]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 3.0 and timeout.read == 10.0


@pytest.mark.parametrize("status", [404, 422])
def test_a_report_the_wms_will_never_take_is_not_tried_again(seeded, wms, caplog, status):
    run_id = _printed_by_platen(seeded)
    _settle(run_id, "done")
    wms.status = status
    with caplog.at_level(logging.DEBUG):
        jobs.report_to_wms(run_id)                # no raise: rq leaves it there
    assert str(status) in caplog.text and run_id in caplog.text
    assert "wms_live_secret" not in caplog.text


@pytest.mark.parametrize("status", [401, 408, 429, 500, 503])
def test_a_report_the_wms_may_take_later_is_tried_again(seeded, wms, caplog, status):
    run_id = _printed_by_platen(seeded)
    _settle(run_id, "done")
    wms.status = status
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match=str(status)) as exc:
        jobs.report_to_wms(run_id)
    assert "wms_live_secret" not in caplog.text and "wms_live_secret" not in str(exc.value)


def test_a_wms_that_cannot_be_reached_is_tried_again(seeded, wms, caplog):
    run_id = _printed_by_platen(seeded)
    _settle(run_id, "done")
    wms.status = httpx.ConnectError("connection refused")
    with caplog.at_level(logging.DEBUG), pytest.raises(httpx.ConnectError) as exc:
        jobs.report_to_wms(run_id)
    assert "wms_live_secret" not in caplog.text and "wms_live_secret" not in str(exc.value)


def test_a_report_with_no_key_says_the_wms_will_likely_refuse_it(seeded, wms, monkeypatch, caplog):
    monkeypatch.setattr(settings_module, "settings",
                        dataclasses.replace(settings_module.settings, wms_key=""))
    run_id = _printed_by_platen(seeded)
    _settle(run_id, "done")
    with caplog.at_level(logging.WARNING):
        jobs.report_to_wms(run_id)
    assert "WMS_KEY" in caplog.text
    assert "Authorization" not in wms.calls[-1]["headers"]


def test_the_id_is_trimmed_like_the_name(seeded):
    assert _key(seeded).post("/intake/wms", json=_job(printer=" dock ")).json()["printer_id"] == "dock"


def _named(admin: TestClient, printer_id: str, name: str, site_id: str | None) -> None:
    assert admin.put(f"/printers/{printer_id}", json={
        "name": name, "dpi": 203, "transport": {"kind": "memory"},
        "site_id": site_id}).status_code == 200


def test_a_printer_name_is_looked_up_among_the_keys_own_sites(two_sites):  # noqa: F811
    _named(two_sites, "gee-bench", "Bench", "gee")
    _named(two_sites, "bal-bench", "Bench", "bal")
    r = _key(two_sites, sites=["bal"]).post("/intake/wms", json=_job(printer="bench"))
    assert r.status_code == 202, r.text
    assert r.json()["printer_id"] == "bal-bench"
    r = _key(two_sites, name="wms-all").post("/intake/wms", json=_job(printer="Bench"))
    assert r.status_code == 422
    assert "more than one printer is called" in r.json()["detail"]


def test_a_printer_named_at_another_site_says_where_it_is(two_sites):  # noqa: F811
    _named(two_sites, "gee-bench", "Bench", "gee")
    r = _key(two_sites, sites=["bal"]).post("/intake/wms", json=_job(printer="Bench"))
    assert r.status_code == 403 and "Geelong" in r.json()["detail"]


def test_a_run_failed_by_the_worker_is_not_put_back_on_a_resend(seeded, queue, monkeypatch):
    c, job = _key(seeded), _job()
    run_id = c.post("/intake/wms", json=job).json()["id"]
    monkeypatch.setattr(jobs.printers, "load", lambda s, printer_id: None)
    jobs.print_run(run_id)
    assert seeded.get(f"/runs/{run_id}").json()["status"] == "failed"
    again = c.post("/intake/wms", json=job)
    assert again.status_code == 202 and again.json()["duplicate"] is True
    assert seeded.get(f"/runs/{run_id}").json()["status"] == "failed"
    assert queue.count == 1


def test_an_integrity_error_that_is_not_a_resend_is_not_hidden(seeded, monkeypatch):
    def clash(*a, **k):
        raise IntegrityError("insert", {}, Exception("some other constraint"))
    monkeypatch.setattr(main, "_queue_run", clash)
    with pytest.raises(IntegrityError):
        _key(seeded).post("/intake/wms", json=_job())


def test_the_published_version_decides_label_or_page(seeded):
    with dbsession.SessionLocal() as s:          # a draft saved as a page since
        s.execute(update(Template).where(Template.id == "carton").values(kind="page"))
        s.commit()
    r = _key(seeded).post("/intake/wms", json=_job(data={**CARTON, "lines": [{}, {}]}))
    assert r.status_code == 202, r.text
    assert r.json()["labels"] == 1


def test_an_unpublished_template_is_named(seeded):
    assert seeded.put("/templates/draft", json={**template_body("blank"),
                                                "id": "draft"}).status_code == 200
    r = _key(seeded).post("/intake/wms", json=_job(template="draft"))
    assert r.status_code == 422 and "publish" in r.json()["detail"]


def test_a_label_sent_to_a_page_printer_is_refused(seeded):
    assert seeded.put("/printers/a4", json={
        "name": "A4", "kind": "page", "transport": {"kind": "memory"}}).status_code == 200
    r = _key(seeded).post("/intake/wms", json=_job(printer="a4"))
    assert r.status_code == 422 and "page printer" in r.json()["detail"]


def test_a_wms_job_follows_a_detour(seeded):
    assert seeded.put("/printers/dock2", json={
        "name": "Dock 2", "dpi": 203, "transport": {"kind": "memory"}}).status_code == 200
    assert seeded.post("/printers/dock/detour", json={"to": "dock2"}).status_code == 200
    r = _key(seeded).post("/intake/wms", json=_job())
    assert r.status_code == 202, r.text
    assert r.json()["detoured_from"] == "dock"
    assert seeded.get(f"/runs/{r.json()['id']}").json()["printer_id"] == "dock2"


def test_a_run_reports_once_when_its_last_try_fails(seeded, queue, transport, monkeypatch):
    run_id = _printed_by_platen(seeded)

    def boom(n):
        raise OSError("stopped answering")
    transport.after_send = boom
    monkeypatch.setattr(jobs, "get_current_job", lambda: type("J", (), {"retries_left": 1})())
    with pytest.raises(OSError):
        jobs.print_run(run_id)
    assert seeded.get(f"/runs/{run_id}").json()["status"] == "retrying"
    assert "app.jobs.report_to_wms" not in _queued(queue)
    monkeypatch.setattr(jobs, "get_current_job", lambda: type("J", (), {"retries_left": 0})())
    with pytest.raises(OSError):
        jobs.print_run(run_id)
    assert _queued(queue).count("app.jobs.report_to_wms") == 1


def test_cancelling_a_blocked_run_queues_the_report(seeded, queue):
    run_id = _printed_by_platen(seeded)
    with dbsession.SessionLocal() as s:
        s.execute(update(PrintRun).where(PrintRun.id == run_id).values(status="blocked"))
        s.commit()
    assert seeded.post(f"/runs/{run_id}/cancel").json()["status"] == "cancelled"
    assert _queued(queue).count("app.jobs.report_to_wms") == 1
