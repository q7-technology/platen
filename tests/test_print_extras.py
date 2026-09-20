"""The three things the print screen promised and didn't do: choosing which
records to print, a separator between jobs, and a PDF to check before you
commit a roll to it."""

from __future__ import annotations

from sqlalchemy import select

from db import session as dbsession
from db.models import RunLabel

PARAMS = {"despatch_date": "2026-09-18"}


# ---------------------------------------------------------- choosing records

def test_the_check_says_what_the_records_are(seeded):
    body = seeded.post("/runs/check", json={"template_id": "carton", "params": PARAMS}).json()

    assert body["records"] == 12
    first = body["record_list"][0]
    assert first["n"] == 1
    assert "Consignee 1" in first["summary"]
    assert "7MB9042101" in first["summary"]


def test_a_record_summary_leaves_out_the_image_columns(seeded):
    """A base64 PNG is four kilobytes of nothing anyone can read."""
    body = seeded.post("/runs/check", json={"template_id": "carton", "params": PARAMS}).json()

    assert all("iVBOR" not in r["summary"] for r in body["record_list"])
    assert all(len(r["summary"]) < 200 for r in body["record_list"])


def test_a_long_run_does_not_send_every_record_back(seeded):
    body = seeded.post("/runs/check",
                       json={"template_id": "carton", "params": PARAMS, "copies": 1}).json()

    assert body["records_capped"] is False
    assert body["record_list"][-1]["n"] == 12


def test_the_records_are_numbered_the_way_the_operator_sees_them(seeded):
    body = seeded.post("/runs/check", json={
        "template_id": "carton", "params": PARAMS, "start_at": 3}).json()

    assert [r["n"] for r in body["record_list"]] == [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    assert body["labels"] == 10


def test_a_record_left_out_stays_in_the_list_so_it_can_be_put_back(seeded):
    """Dropping one off the list would make unticking a one-way door."""
    body = seeded.post("/runs/check", json={
        "template_id": "carton", "params": PARAMS, "skip_rows": [2, 5]}).json()

    assert [r["n"] for r in body["record_list"]] == list(range(1, 13))
    assert [r["n"] for r in body["record_list"] if not r["included"]] == [2, 5]
    assert body["records"] == 10, "the count is what will print"
    assert body["labels"] == 10


# --------------------------------------------------------------- a separator

def test_a_separator_goes_in_front_of_the_job(seeded):
    r = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                   "params": PARAMS, "separator": True})

    assert r.status_code == 202
    assert r.json()["labels"] == 13
    with dbsession.SessionLocal() as s:
        first = s.scalars(select(RunLabel).where(RunLabel.run_id == r.json()["id"])
                          .order_by(RunLabel.seq)).all()[0]
    assert first.seq == 1
    assert r.json()["id"] in first.zpl
    assert "12 labels" in first.zpl


def test_the_separator_is_the_same_size_as_the_stock(seeded):
    run = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                     "params": PARAMS, "separator": True}).json()

    with dbsession.SessionLocal() as s:
        first = s.scalars(select(RunLabel).where(RunLabel.run_id == run["id"])
                          .order_by(RunLabel.seq)).all()[0]
    assert "^PW799" in first.zpl and "^LL1199" in first.zpl


def test_without_asking_there_is_no_separator(seeded):
    run = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                     "params": PARAMS}).json()

    assert run["labels"] == 12


def test_the_check_counts_the_separator_too(seeded):
    body = seeded.post("/runs/check", json={
        "template_id": "carton", "params": PARAMS, "separator": True}).json()

    assert body["labels"] == 13
    assert body["record_list"][0]["n"] == 1    # the separator is not a record


# --------------------------------------------------------------------- a PDF

def test_a_run_can_be_had_as_a_pdf_before_it_is_printed(seeded):
    r = seeded.post("/runs/preview.pdf", json={"template_id": "carton", "params": PARAMS})

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert r.headers["content-disposition"].endswith('.pdf"')


def test_the_pdf_has_a_page_per_label(seeded):
    r = seeded.post("/runs/preview.pdf", json={"template_id": "carton", "params": PARAMS,
                                               "copies": 2})

    assert r.content.count(b"/Type /Page") >= 24


def test_a_very_long_run_gives_the_first_pages_and_says_so(seeded):
    r = seeded.post("/runs/preview.pdf", json={"template_id": "carton", "params": PARAMS,
                                               "copies": 10})

    assert r.status_code == 200
    assert r.headers["x-platen-pages"] == "50"
    assert r.headers["x-platen-labels"] == "120"


def test_a_binding_that_cannot_resolve_does_not_produce_a_pdf(seeded):
    from tests.conftest import template_body
    body = template_body("fail")
    seeded.put("/templates/carton", json=body)
    seeded.post("/templates/carton/publish")

    r = seeded.post("/runs/preview.pdf", json={"template_id": "carton", "params": PARAMS})

    assert r.status_code == 422
    assert "row 7" in r.json()["detail"]


def test_the_pdf_pages_are_the_size_of_the_label(seeded):
    r = seeded.post("/runs/preview.pdf", json={"template_id": "carton", "params": PARAMS})

    # the label is 100 x 150 mm; a PDF point is 1/72 inch
    assert b"/MediaBox [ 0 0 283" in r.content or b"/MediaBox [0 0 283" in r.content


def test_an_operator_may_take_the_pdf(operator):
    assert operator.post("/runs/preview.pdf",
                         json={"template_id": "carton", "params": PARAMS}).status_code == 200
