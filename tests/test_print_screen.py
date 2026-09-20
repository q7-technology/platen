"""What the operator's print screen needs from the API."""

from __future__ import annotations

from sqlalchemy import select

from db import session as dbsession
from db.models import PrintRun
from tests.conftest import template_body

PARAMS = {"despatch_date": "2026-09-18"}


def _runs() -> int:
    with dbsession.SessionLocal() as s:
        return len(s.scalars(select(PrintRun)).all())


def test_check_reports_counts_and_warnings_without_creating_a_run(seeded, queue):
    r = seeded.post("/runs/check", json={"template_id": "carton", "params": PARAMS, "copies": 2})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["records"] == 12
    assert body["labels"] == 24
    assert body["warnings"] == ["row 7: logo: no image in this row"]
    assert body["template_version"] == 1
    assert isinstance(body["elapsed_ms"], int)
    assert queue.count == 0
    assert _runs() == 0


def test_check_reports_a_render_failure_the_same_way_as_a_run(seeded):
    seeded.put("/templates/carton", json=template_body("fail"))
    seeded.post("/templates/carton/publish")

    r = seeded.post("/runs/check", json={"template_id": "carton", "params": PARAMS})

    assert r.status_code == 422
    assert "row 7" in r.json()["detail"] and "logo" in r.json()["detail"]


def test_skipped_records_are_left_out_of_the_run(seeded):
    r = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                   "params": PARAMS, "skip_rows": [7]})

    assert r.status_code == 202, r.text
    assert r.json()["labels"] == 11
    assert r.json()["warnings"] == []


def test_preview_can_render_from_the_published_version(seeded):
    draft = template_body("blank")
    draft["elements"][0]["value"] = "DRAFT ONLY"
    seeded.put("/templates/carton", json=draft)

    from_draft = seeded.post("/templates/carton/preview.zpl", json={"params": PARAMS})
    from_published = seeded.post("/templates/carton/preview.zpl",
                                 json={"params": PARAMS, "published": True})

    assert "DRAFT ONLY" in from_draft.text
    assert "DRAFT ONLY" not in from_published.text
    assert "Consignee 1" in from_published.text


def test_preview_png_from_published_version_differs_from_the_draft(seeded):
    draft = template_body("blank")
    draft["elements"][0]["value"] = "DRAFT ONLY"
    seeded.put("/templates/carton", json=draft)

    from_draft = seeded.post("/templates/carton/preview.png", json={"params": PARAMS, "record": 2})
    from_published = seeded.post("/templates/carton/preview.png",
                                 json={"params": PARAMS, "record": 2, "published": True})

    assert from_published.status_code == 200
    assert from_published.headers["content-type"] == "image/png"
    assert from_published.content.startswith(b"\x89PNG")
    assert from_published.content != from_draft.content


def test_query_detail_lists_its_parameters(seeded):
    r = seeded.get("/queries/cartons")
    assert r.status_code == 200
    body = r.json()
    assert body["datasource_id"] == "warehouse"
    assert body["parameters"] == [{
        "name": "despatch_date", "type": "date", "default": None,
        "ask_at_print": True, "label": "",
    }]


def test_template_list_says_which_query_it_binds(seeded):
    seeded.put("/templates/unpublished", json={**template_body("blank"), "id": "unpublished"})
    rows = {t["id"]: t for t in seeded.get("/templates").json()}
    assert rows["carton"]["version"] == 1
    assert rows["carton"]["query"] == "cartons"
    assert rows["unpublished"]["version"] == 0


def test_print_screen_is_served(client):
    r = client.get("/studio/print")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Print run" in r.text
    assert client.get("/studio/static/q7-logo-128.png").status_code == 200
