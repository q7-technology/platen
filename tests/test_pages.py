"""Page printing: packing slips on an office printer, as PDFs."""

from __future__ import annotations

import sqlite3

import pytest

from app import health, jobs, pages
from db import session as dbsession
from tests.test_transports import Listener

PARAMS = {"day": "2026-10-06"}


@pytest.fixture
def orders(tmp_path) -> str:
    """Two orders: SO-1 is long enough to run onto a second page."""
    path = tmp_path / "orders.sqlite"
    con = sqlite3.connect(path)
    con.execute("create table lines (order_no text, customer text, sku text, descr text,"
                " qty integer, day text)")
    for i in range(1, 51):
        con.execute("insert into lines values (?,?,?,?,?,?)",
                    ("SO-1", "Coles DC <img src='/etc/passwd'/>", f"BF-{i:03d}",
                     f"Brake fluid {i}", i, "2026-10-06"))
    con.execute("insert into lines values ('SO-2','IGA Wendouree','WB-01','Wipers',6,'2026-10-06')")
    con.commit()
    con.close()
    return f"sqlite:///{path}"


def slip(**over) -> dict:
    return {
        "id": "slip", "name": "Packing slip", "datasource": "erp", "query": "lines",
        "group_by": "order_no",
        "header": [{"kind": "text", "value": "Packing slip {{ order_no }}", "size_pt": 16,
                    "bold": True},
                   {"kind": "text", "value": "{{ customer }}"}],
        "body": [{"kind": "barcode", "value": "{{ order_no }}"},
                 {"kind": "table", "columns": [
                     {"title": "SKU", "value": "{{ sku }}", "width_mm": 35},
                     {"title": "Description", "value": "{{ descr }}"},
                     {"title": "Qty", "value": "{{ qty }}", "width_mm": 18, "align": "right"}]}],
        "footer": [{"kind": "text", "value": "Page {{ page }} of {{ pages }}", "align": "right"}],
        **over,
    }


@pytest.fixture
def office(client, orders, transport) -> object:
    """A published packing slip, an office printer, and the seeded-style query."""
    assert client.put("/datasources/erp", json={"name": "ERP", "url": orders}).status_code == 200
    assert client.put("/queries/lines", json={
        "datasource_id": "erp", "name": "Lines for a day",
        "sql": "select * from lines where day = :day order by order_no, sku",
        "parameters": [{"name": "day", "type": "date", "ask_at_print": True}],
    }).status_code == 200
    r = client.put("/pages/slip", json=slip())
    assert r.status_code == 200, r.text
    assert client.post("/templates/slip/publish").status_code == 200
    for pid, kind in (("laser", "page"), ("laser2", "page"), ("zebra", "label")):
        assert client.put(f"/printers/{pid}", json={
            "name": pid.title(), "kind": kind, "transport": {"kind": "memory"}}).status_code == 200
    return client


def _run(client, printer_id="laser"):
    return client.post("/runs", json={"template_id": "slip", "printer_id": printer_id,
                                      "params": PARAMS})


def test_a_page_template_round_trips(office):
    got = office.get("/pages/slip").json()
    assert got["kind"] == "page" and got["group_by"] == "order_no"
    assert got["body"][1]["columns"][2]["align"] == "right"
    listed = {t["id"]: t for t in office.get("/templates").json()}
    assert listed["slip"]["kind"] == "page" and listed["slip"]["size_mm"] == [210.0, 297.0]


def test_labels_and_pages_dont_cross(office, seeded):
    assert office.get("/templates/slip").status_code == 409
    assert office.put("/pages/carton", json=slip(id="carton")).status_code == 409
    assert office.get("/pages/carton").status_code == 409


def test_the_preview_is_the_pdf_that_prints(office):
    r = office.post("/pages/slip/preview.png?page=2", json={"params": PARAMS})
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert r.headers["x-platen-pages"] == "2"             # 50 lines run onto page 2
    assert r.headers["x-platen-documents"] == "2"
    assert office.post("/pages/slip/preview.png?page=3", json={"params": PARAMS}).status_code == 422
    one = office.post("/pages/slip/preview.png", json={"params": PARAMS, "record": 1})
    assert one.headers["x-platen-pages"] == "1"           # SO-2 is one line
    pdf = office.post("/pages/slip/preview.pdf", json={"params": PARAMS})
    assert pdf.content.startswith(b"%PDF")


def test_the_print_screen_preview_works_for_pages(office):
    r = office.post("/templates/slip/preview.png", json={"params": PARAMS, "published": True})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"


def test_a_page_run_prints_one_pdf_per_document(office, transport):
    check = office.post("/runs/check", json={"template_id": "slip", "params": PARAMS}).json()
    assert check["kind"] == "page" and check["documents"] == 2 and check["pages"] == 3
    run_id = _run(office).json()["id"]
    jobs.print_run(run_id)
    assert len(transport.sent) == 2
    assert all(doc.startswith(b"%PDF") for doc in transport.sent)
    st = office.get(f"/runs/{run_id}").json()
    assert st["status"] == "done" and st["printed"] == 2 and st["total"] == 2


def test_data_never_becomes_markup(office, transport):
    run_id = _run(office).json()["id"]
    jobs.print_run(run_id)                        # the <img src> customer printed as text
    assert len(transport.sent) == 2


def test_a_page_run_wants_a_page_printer(office, seeded):
    r = _run(office, "zebra")
    assert r.status_code == 422 and "pick a page printer" in r.json()["detail"]
    r = seeded.post("/runs", json={"template_id": "carton", "printer_id": "laser",
                                   "params": {"despatch_date": "2026-09-18"}})
    assert r.status_code == 422 and "pick a label printer" in r.json()["detail"]


def test_a_page_run_moves_between_page_printers_only(office):
    run_id = _run(office).json()["id"]
    assert office.post(f"/runs/{run_id}/move", json={"printer_id": "zebra"}).status_code == 422
    assert office.post(f"/runs/{run_id}/move", json={"printer_id": "laser2"}).status_code == 200
    assert office.post("/printers/laser/detour", json={"to": "zebra"}).status_code == 422
    assert office.post("/printers/laser/detour", json={"to": "laser2"}).status_code == 200


def test_a_missing_column_names_the_block(office):
    bad = slip()
    bad["body"][1]["columns"][0]["value"] = "{{ part_no }}"
    office.put("/pages/slip", json=bad)
    r = office.post("/pages/slip/preview.png", json={"params": PARAMS})
    assert r.status_code == 422
    assert "body block 2 (table), column 'SKU'" in r.json()["detail"]
    assert "part_no" in r.json()["detail"]


def test_group_by_a_column_that_isnt_there(office):
    office.put("/pages/slip", json=slip(group_by="order_number"))
    r = office.post("/pages/slip/preview.png", json={"params": PARAMS})
    assert r.status_code == 422 and "order_number" in r.json()["detail"]


def test_a_page_with_no_query_is_one_document():
    t = pages.PageTemplate(id="memo", name="Memo", body=[pages.TextBlock(value="Hello")])
    docs, warnings = pages.render_run(t, [{}])
    assert len(docs) == 1 and warnings == []


def test_an_office_printer_is_never_sent_zpl(client):
    """~HQES on a laser's port 9100 prints the letters ~HQES on a page."""
    laser = Listener(reply=b"")
    try:
        assert client.put("/printers/hp", json={
            "name": "HP", "kind": "page",
            "transport": {"kind": "tcp", "host": "127.0.0.1", "port": laser.port}}).status_code == 200
        with dbsession.SessionLocal() as s:
            health.check_all(s)
    finally:
        laser.close()
    assert laser.received == []
    assert client.get("/printers/hp").json()["health"]["state"] == "ready"


def test_an_office_printer_test_is_a_page(office, transport):
    assert office.post("/printers/laser/test").json()["sent"] == "test page"
    assert transport.sent[-1].startswith(b"%PDF")


def test_cups_filters_a_pdf_but_not_zpl(client):
    client.put("/printers/q1", json={"name": "Q", "kind": "page",
                                      "transport": {"kind": "cups", "queue": "office"}})
    client.put("/printers/q2", json={"name": "Q", "kind": "label",
                                      "transport": {"kind": "cups", "queue": "zebra"}})
    assert client.get("/printers/q1").json()["transport"]["raw"] is False
    assert "raw" not in client.get("/printers/q2").json()["transport"]


def test_an_agent_takes_labels_only(client):
    r = client.put("/printers/a1", json={"name": "A", "kind": "page",
                                         "transport": {"kind": "agent", "agent_id": "x"}})
    assert r.status_code == 422
