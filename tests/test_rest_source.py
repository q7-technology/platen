"""A data source that is an HTTP endpoint rather than a database.

The same promises as the SQL side: read-only by construction (only GET is ever
issued), and an operator's answer is encoded into the request, never pasted
into it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import datasources
from db import session as dbsession
from db.models import DataSource, QueryParameter, SavedQuery

SEEN: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    payload: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        SEEN.append({"path": self.path, "headers": dict(self.headers)})
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        SEEN.append({"path": self.path, "method": "POST"})
        self.send_response(405)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def api():
    SEEN.clear()
    Handler.payload = {"data": {"items": [
        {"consignment_no": "7MB9042198", "consignee_name": "Coles DC Truganina", "qty": 24},
        {"consignment_no": "7MB9042199", "consignee_name": "IGA Ballarat", "qty": 6},
    ]}}
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _source(db, api: str, headers: dict | None = None) -> None:
    with dbsession.SessionLocal() as s:
        s.add(DataSource(id="shopify", name="Shopify", kind="rest", url=api,
                         headers=headers or {}))
        s.commit()


def _query(db, path: str, row_path: str = "data.items", params: list[str] | None = None) -> None:
    with dbsession.SessionLocal() as s:
        s.add(SavedQuery(id="orders", datasource_id="shopify", name="Orders",
                         sql=path, row_path=row_path,
                         parameters=[QueryParameter(name=p, type="text", position=i)
                                     for i, p in enumerate(params or [])]))
        s.commit()


def _run(params: dict, limit: int | None = None):
    with dbsession.SessionLocal() as s:
        return datasources.run(s, datasources.saved_query(s, "orders"), params, limit=limit)


def test_rows_come_back_as_plain_dicts(db, api):
    _source(db, api)
    _query(db, "/orders.json")

    rows = _run({})

    assert [r["consignment_no"] for r in rows] == ["7MB9042198", "7MB9042199"]


def test_an_operators_answer_is_encoded_into_the_request_not_pasted_in(db, api):
    """A despatch date with an ampersand in it must not become another
    parameter, the same way it must not become more SQL."""
    _source(db, api)
    _query(db, "/orders/:site.json", params=["site", "status"])

    _run({"site": "truganina/east&admin=1", "status": "ready to ship"})

    path = SEEN[-1]["path"]
    assert "truganina%2Feast%26admin%3D1" in path
    assert "admin=1" not in path.replace("%26admin%3D1", "")
    assert "status=ready+to+ship" in path or "status=ready%20to%20ship" in path


def test_a_parameter_not_in_the_path_is_sent_as_a_query_string(db, api):
    _source(db, api)
    _query(db, "/orders.json", params=["despatch_date"])

    _run({"despatch_date": "2026-09-18"})

    assert "despatch_date=2026-09-18" in SEEN[-1]["path"]


def test_the_rows_can_be_somewhere_inside_the_response(db, api):
    _source(db, api)
    _query(db, "/orders.json", row_path="data.items")

    assert len(_run({})) == 2


def test_a_bare_list_needs_no_path_at_all(db, api):
    Handler.payload = [{"sku": "BF-2201"}]
    _source(db, api)
    _query(db, "/orders.json", row_path="")

    assert _run({}) == [{"sku": "BF-2201"}]


def test_a_limit_is_applied_even_though_the_endpoint_has_no_idea(db, api):
    _source(db, api)
    _query(db, "/orders.json")

    assert len(_run({}, limit=1)) == 1


def test_a_token_is_sent_but_never_handed_to_the_browser(db, api, client):
    _source(db, api, headers={"Authorization": "Bearer sh_live_secret"})
    _query(db, "/orders.json")
    _run({})

    assert SEEN[-1]["headers"]["Authorization"] == "Bearer sh_live_secret"
    shown = client.get("/datasources/shopify").json()
    assert "sh_live_secret" not in json.dumps(shown)
    assert shown["headers"]["Authorization"] == "***"


def test_saving_a_masked_header_back_keeps_the_token(db, api, client):
    _source(db, api, headers={"Authorization": "Bearer sh_live_secret"})

    client.put("/datasources/shopify", json={
        "name": "Shopify orders", "kind": "rest", "url": api,
        "headers": {"Authorization": "***"}})

    with dbsession.SessionLocal() as s:
        assert s.get(DataSource, "shopify").headers["Authorization"] == "Bearer sh_live_secret"


def test_the_columns_are_found_without_running_a_real_query(db, api):
    _source(db, api)
    _query(db, "/orders.json")

    with dbsession.SessionLocal() as s:
        columns = datasources.columns(s, datasources.saved_query(s, "orders"))

    assert columns == ["consignment_no", "consignee_name", "qty"]


def test_an_endpoint_that_answers_with_rubbish_says_so(db, api):
    Handler.payload = {"data": {"items": "not a list at all"}}
    _source(db, api)
    _query(db, "/orders.json")

    with pytest.raises(ValueError) as exc:
        _run({})

    assert "data.items" in str(exc.value)


def test_only_get_is_ever_issued(db, api):
    _source(db, api)
    _query(db, "/orders.json")
    _run({})

    assert all(seen.get("method") != "POST" for seen in SEEN)
