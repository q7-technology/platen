"""The demo seed has to keep working as the schema moves, or the first thing
somebody tries is the thing that's broken."""

from __future__ import annotations

from app import demo
from db import models
from db import session as dbsession


def _seed(tmp_path) -> list[str]:
    demo.build_warehouse(tmp_path / "warehouse.sqlite")
    with dbsession.SessionLocal() as s:
        return demo.seed(s, tmp_path / "warehouse.sqlite")


def test_every_demo_template_previews_from_its_published_version(client, tmp_path):
    _seed(tmp_path)
    for spec in demo.LABELS:
        r = client.post(f"/templates/{spec['id']}/preview.png", json={"published": True})
        assert r.status_code == 200, (spec["id"], r.text)
    for spec in demo.PAGES:
        r = client.post(f"/pages/{spec['id']}/preview.png", json={"published": True})
        assert r.status_code == 200, (spec["id"], r.text)


def test_a_customer_with_no_logo_still_gets_a_label(client, tmp_path):
    _seed(tmp_path)
    r = client.post("/templates/demo-shipping/preview.png",
                    json={"published": True, "params": {"order_no": "ORD-10424"}})
    assert r.status_code == 200, r.text


def test_seeding_twice_publishes_nothing_new(db, tmp_path):
    _seed(tmp_path)
    again = _seed(tmp_path)
    assert all(line.endswith("v1") for line in again if line.startswith(("label", "page")))
    with dbsession.SessionLocal() as s:
        assert s.query(models.TemplateVersion).count() == len(demo.LABELS) + len(demo.PAGES)


def test_the_demo_source_is_opened_read_only(db, tmp_path):
    _seed(tmp_path)
    with dbsession.SessionLocal() as s:
        assert "mode=ro" in s.get(models.DataSource, demo.DATASOURCE).url
