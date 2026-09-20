"""The setup screens: connections, saved queries and printers."""

from __future__ import annotations

from tests.conftest import template_body

PARAMS = {"despatch_date": "2026-09-18"}


# ------------------------------------------------------------- data sources

def test_a_connection_never_hands_its_password_to_the_browser(client, tmp_path):
    secret = "postgresql+psycopg://platen_ro:hunter2@10.20.1.9:5432/wms"
    client.put("/datasources/wms", json={"name": "wms_prod", "url": secret})

    shown = client.get("/datasources/wms").json()["url"]

    assert "hunter2" not in shown
    assert "platen_ro" in shown and "10.20.1.9:5432/wms" in shown


def test_saving_a_connection_back_unchanged_keeps_the_password(client):
    secret = "postgresql+psycopg://platen_ro:hunter2@10.20.1.9:5432/wms"
    client.put("/datasources/wms", json={"name": "wms_prod", "url": secret})

    masked = client.get("/datasources/wms").json()["url"]
    client.put("/datasources/wms", json={"name": "Warehouse", "url": masked})

    assert client.get("/datasources/wms/reveal").json()["url"] == secret
    assert client.get("/datasources/wms").json()["name"] == "Warehouse"


def test_a_connection_lists_the_queries_that_use_it(seeded):
    body = seeded.get("/datasources/warehouse").json()
    assert body["queries"] == ["cartons"]


def test_a_connection_in_use_is_not_deleted_silently(seeded):
    r = seeded.delete("/datasources/warehouse")

    assert r.status_code == 409
    assert "cartons" in r.json()["detail"]
    assert seeded.get("/datasources/warehouse").status_code == 200


def test_a_query_in_use_by_a_template_is_not_deleted_silently(seeded):
    r = seeded.delete("/queries/cartons")

    assert r.status_code == 409
    assert "carton" in r.json()["detail"]


def test_queries_can_be_listed_for_one_connection(seeded, customer_db):
    seeded.put("/datasources/other", json={"name": "Other", "url": customer_db})
    seeded.put("/queries/everything", json={
        "datasource_id": "other", "name": "Everything", "sql": "select * from cartons"})

    ours = [q["id"] for q in seeded.get("/queries", params={"datasource_id": "warehouse"}).json()]
    assert ours == ["cartons"]


def test_a_query_preview_says_which_column_holds_an_image(seeded):
    body = seeded.post("/queries/cartons/preview",
                       json={"params": PARAMS, "limit": 3}).json()

    assert body["count"] == 3
    assert isinstance(body["elapsed_ms"], int)
    kinds = {f["name"]: f["type"] for f in body["fields"]}
    assert kinds["logo"] == "base64_image"
    assert kinds["consignee_name"] == "str"


def test_a_broken_connection_reports_why_rather_than_a_500(client):
    client.put("/datasources/nowhere", json={
        "name": "Nowhere", "url": "postgresql+psycopg://nobody@127.0.0.1:1/none"})

    body = client.post("/datasources/nowhere/test").json()

    assert body["ok"] is False
    assert body["error"]


# ----------------------------------------------------------------- printers

def test_a_printer_round_trips_its_transport_for_editing(client):
    client.put("/printers/dock2", json={
        "name": "Dock 2", "model": "ZT411", "dpi": 300,
        "transport": {"kind": "tcp", "host": "10.20.4.31", "port": 9100}})

    body = client.get("/printers/dock2").json()

    assert body["transport"] == {"kind": "tcp", "host": "10.20.4.31", "port": 9100}
    assert body["model"] == "ZT411" and body["dpi"] == 300


def test_printers_are_probed_in_parallel_not_one_after_another(client, transport):
    transport.probe_delay = 0.05
    for i in range(4):
        client.put(f"/printers/p{i}", json={"name": f"P{i}", "transport": {"kind": "memory"}})

    client.get("/printers")

    assert len(set(transport.probe_threads)) > 1, "printers were probed one at a time"


def test_the_printer_list_can_skip_probing(client, transport):
    client.put("/printers/p0", json={"name": "P0", "transport": {"kind": "memory"}})

    body = client.get("/printers", params={"probe": "false"}).json()

    assert body[0]["online"] is None
    assert transport.probe_threads == []


def test_a_printer_with_history_is_not_deleted_silently(seeded):
    run = seeded.post("/runs", json={"template_id": "carton", "printer_id": "dock",
                                     "params": PARAMS}).json()["id"]

    r = seeded.delete("/printers/dock")

    assert r.status_code == 409
    assert run in r.json()["detail"] or "print run" in r.json()["detail"]


def test_an_unused_printer_is_deleted(client):
    client.put("/printers/spare", json={"name": "Spare", "transport": {"kind": "memory"}})

    assert client.delete("/printers/spare").status_code == 204
    assert client.get("/printers/spare").status_code == 404


# ------------------------------------------------------------------ screens

def test_the_setup_screens_are_served(client):
    for path in ("/studio/data", "/studio/printers"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/html")
    assert client.get("/studio/static/studio/studio.css").status_code == 200


def test_a_query_can_be_saved_again_with_the_same_parameters(seeded):
    """Editing the SQL and pressing save again is the ordinary case."""
    body = seeded.get("/queries/cartons").json()

    r = seeded.put("/queries/cartons", json={
        "datasource_id": body["datasource_id"], "name": body["name"],
        "sql": body["sql"] + " -- edited",
        "parameters": [{"name": "despatch_date", "type": "date", "ask_at_print": True}],
    })

    assert r.status_code == 200, r.text
    after = seeded.get("/queries/cartons").json()
    assert after["sql"].endswith("-- edited")
    assert [p["name"] for p in after["parameters"]] == ["despatch_date"]


def test_a_parameter_removed_from_a_query_is_gone(seeded):
    seeded.put("/queries/cartons", json={
        "datasource_id": "warehouse", "name": "Cartons", "sql": "select * from cartons",
        "parameters": [],
    })

    assert seeded.get("/queries/cartons").json()["parameters"] == []
