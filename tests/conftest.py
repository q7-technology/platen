"""One SQLite file stands in for Platen's own Postgres, a second one for the
customer's database, and fakeredis for the queue. Nothing here needs Docker."""

from __future__ import annotations

import base64
import io
import sqlite3
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from rq import Queue

from app import jobs, printers
from app.main import app
from db import session as dbsession
from db.models import Base


def small_png(seed: int = 0) -> bytes:
    img = Image.new("L", (40, 24), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([2 + seed % 3, 2, 18, 21], fill=0)
    d.line([20, 2, 38, 21], fill=0, width=3)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class RecordingTransport:
    """Stands in for a printer: keeps every write, can flip a cancel flag."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.after_send: Any = None

    def send(self, data: bytes) -> None:
        self.sent.append(data)
        if self.after_send:
            self.after_send(len(self.sent))

    def probe(self) -> bool:
        return True


@pytest.fixture
def transport(monkeypatch) -> RecordingTransport:
    t = RecordingTransport()
    monkeypatch.setitem(printers.TRANSPORTS, "memory", lambda cfg: t)
    return t


@pytest.fixture
def queue() -> Queue:
    conn = fakeredis.FakeStrictRedis()
    jobs.bind(conn)
    return jobs.queue()


@pytest.fixture
def db(tmp_path):
    engine = dbsession.make_engine(f"sqlite:///{tmp_path / 'platen.sqlite'}")
    Base.metadata.create_all(engine)
    dbsession.bind(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(db, queue, transport) -> TestClient:
    return TestClient(app)


@pytest.fixture
def customer_db(tmp_path) -> str:
    """Twelve cartons. Row 7 has no logo — the awkward one."""
    path = tmp_path / "warehouse.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "create table cartons (consignment_no text, consignee_name text,"
        " despatch_date text, logo text)"
    )
    for i in range(1, 13):
        logo = None if i == 7 else base64.b64encode(small_png(i)).decode()
        con.execute(
            "insert into cartons values (?, ?, ?, ?)",
            (f"7MB90421{i:02d}", f"Consignee {i}", "2026-09-18", logo),
        )
    con.commit()
    con.close()
    return f"sqlite:///{path}"


def template_body(on_missing: str) -> dict[str, Any]:
    return {
        "id": "carton",
        "name": "Carton",
        "width_mm": 100,
        "height_mm": 150,
        "dpi": 203,
        "datasource": "warehouse",
        "query": "cartons",
        "elements": [
            {"kind": "text", "name": "consignee", "box": {"x": 6, "y": 6, "w": 88, "h": 10},
             "value": "{{ consignee_name }}", "height_pt": 14},
            {"kind": "barcode", "name": "consignment_barcode",
             "box": {"x": 6, "y": 60, "w": 88, "h": 20}, "value": "{{ consignment_no }}"},
            {"kind": "image", "name": "logo", "box": {"x": 6, "y": 100, "w": 40, "h": 24},
             "value": "{{ logo }}", "on_missing": on_missing, "dither": False},
        ],
    }


@pytest.fixture
def seeded(client, customer_db) -> TestClient:
    """A datasource, a saved query with one print-time parameter, a printer
    on the recording transport, and a published template."""
    assert client.put("/datasources/warehouse", json={
        "name": "Warehouse", "url": customer_db}).status_code == 200
    assert client.put("/queries/cartons", json={
        "datasource_id": "warehouse", "name": "Cartons for a despatch date",
        "sql": "select * from cartons where despatch_date = :despatch_date order by consignment_no",
        "parameters": [{"name": "despatch_date", "type": "date", "ask_at_print": True}],
    }).status_code == 200
    assert client.put("/printers/dock", json={
        "name": "Dock 1", "model": "ZT411", "dpi": 203,
        "transport": {"kind": "memory"}}).status_code == 200
    assert client.put("/templates/carton", json=template_body("blank")).status_code == 200
    assert client.post("/templates/carton/publish").status_code == 200
    return client
