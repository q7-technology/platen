from __future__ import annotations

import base64
import io
import re

from PIL import Image
from sqlalchemy import select

from app import images, jobs
from db import session as dbsession
from db.models import PrintRun, RunLabel, RunWarning, TemplateVersion
from tests.conftest import small_png

PARAMS = {"despatch_date": "2026-09-18"}


def _count(model) -> int:
    with dbsession.SessionLocal() as s:
        return len(s.scalars(select(model)).all())


def test_render_failure_queues_nothing(seeded, queue, transport):
    from tests.conftest import template_body
    seeded.put("/templates/carton", json=template_body("fail"))
    seeded.post("/templates/carton/publish")

    r = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock", "params": PARAMS})

    assert r.status_code == 422
    assert "row 7" in r.json()["detail"] and "logo" in r.json()["detail"]
    assert queue.count == 0
    assert _count(PrintRun) == 0
    assert _count(RunLabel) == 0
    assert transport.sent == []


def test_null_image_is_a_warning_not_an_error(seeded, queue):
    r = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock", "params": PARAMS})

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["labels"] == 12
    assert body["warnings"] == ["row 7: logo: no image in this row"]
    assert queue.count == 1
    with dbsession.SessionLocal() as s:
        warnings = s.scalars(select(RunWarning)).all()
        assert [(w.row_no, w.message) for w in warnings] == [(7, "logo: no image in this row")]
        labels = s.scalars(select(RunLabel).order_by(RunLabel.seq)).all()
        assert len(labels) == 12
        assert "^GFA" in labels[0].zpl
        assert "^GFA" not in labels[6].zpl


def test_base64_png_round_trips_through_gfa():
    png = small_png()
    original = Image.open(io.BytesIO(png)).convert("1")

    g = images.to_graphic(base64.b64encode(png).decode(), 40, 24, dither=False)
    m = re.fullmatch(r"\^GFA,(\d+),(\d+),(\d+),([0-9A-F]+)", g.zpl)
    assert m, g.zpl[:40]
    total, _, row_bytes, hexdata = int(m[1]), m[2], int(m[3]), m[4]
    assert row_bytes == 5 and total == 5 * 24

    packed = bytes.fromhex(hexdata)
    back = Image.new("1", (40, 24), 255)
    for y in range(24):
        for x in range(40):
            burn = packed[y * row_bytes + (x >> 3)] & (0x80 >> (x & 7))
            back.putpixel((x, y), 0 if burn else 255)
    assert back.convert("L").tobytes() == original.convert("L").tobytes()


def test_publish_writes_a_new_version_and_the_run_records_it(seeded):
    from tests.conftest import template_body

    body = template_body("blank")
    body["elements"][0]["height_pt"] = 18
    seeded.put("/templates/carton", json=body)
    assert seeded.post("/templates/carton/publish").json()["version"] == 2

    with dbsession.SessionLocal() as s:
        versions = s.scalars(select(TemplateVersion).order_by(TemplateVersion.version)).all()
        assert [v.version for v in versions] == [1, 2]
        assert versions[0].definition["elements"][0]["height_pt"] == 14
        assert versions[1].definition["elements"][0]["height_pt"] == 18

    r = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock", "params": PARAMS})
    assert r.status_code == 202, r.text
    assert r.json()["template_version"] == 2
    assert seeded.get(f"/runs/{r.json()['id']}").json()["template_version"] == 2


def test_worker_checks_cancel_between_labels(seeded, transport):
    run_id = seeded.post("/runs", json={
        "template_id": "carton", "printer_id": "dock", "params": PARAMS}).json()["id"]

    def cancel_after_third(n: int) -> None:
        if n == 3:
            seeded.post(f"/runs/{run_id}/cancel")

    transport.after_send = cancel_after_third
    jobs.print_run(run_id)

    status = seeded.get(f"/runs/{run_id}").json()
    assert len(transport.sent) == 3
    assert status["status"] == "cancelled"
    assert status["printed"] == 3
    assert status["total"] == 12
