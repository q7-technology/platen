"""What a data source does when things are not fine."""

from __future__ import annotations

import pytest

from app import datasources
from app.datasources import DataSource, Parameter, SavedQuery
from db import session as dbsession
from db.models import DataSource as SourceRow


def test_a_database_that_is_not_there_probes_as_a_failure(client):
    client.put("/datasources/gone", json={
        "name": "Gone", "url": "postgresql+psycopg://nobody@127.0.0.1:1/none"})

    body = client.post("/datasources/gone/test").json()

    assert body["ok"] is False
    assert body["error"]


def test_an_endpoint_that_answers_badly_is_not_ok(client):
    client.put("/datasources/dead", json={
        "name": "Dead", "kind": "rest", "url": "http://127.0.0.1:1/nothing"})

    body = client.post("/datasources/dead/test").json()

    assert body["ok"] is False


def test_asking_for_a_source_that_was_deleted_gives_nothing_not_a_crash(db):
    with dbsession.SessionLocal() as s:
        assert datasources.datasource(s, "never-existed") is None
        assert datasources.saved_query(s, "never-existed") is None


def test_a_query_whose_source_has_gone_says_which_one(db):
    query = SavedQuery(name="orphan", datasource="vanished", sql="select 1")

    with dbsession.SessionLocal() as s, pytest.raises(ValueError) as exc:
        datasources.run(s, query, {})

    assert "vanished" in str(exc.value)
    assert "orphan" in str(exc.value)


def test_a_parameter_nobody_answered_is_named(db, customer_db):
    with dbsession.SessionLocal() as s:
        s.add(SourceRow(id="wms", name="wms", url=customer_db))
        s.commit()
    query = SavedQuery(name="cartons", datasource="wms",
                       sql="select * from cartons where despatch_date = :d",
                       parameters=[Parameter(name="d"), Parameter(name="site")])

    with dbsession.SessionLocal() as s, pytest.raises(ValueError) as exc:
        datasources.run(s, query, {"d": "2026-09-18"})

    assert "site" in str(exc.value)
    assert "d" not in str(exc.value).replace("despatch", "").replace("data", "")


def test_a_connection_string_that_makes_no_sense_is_shown_as_typed():
    """Masking should never eat what somebody has to correct."""
    assert datasources.masked("not a url at all") == "not a url at all"
    assert datasources.unmasked("not a url at all", "also nonsense") == "not a url at all"


def test_a_password_survives_a_url_that_cannot_be_parsed():
    stored = "postgresql+psycopg://user:secret@host/db"

    assert datasources.unmasked("still nonsense", stored) == "still nonsense"


def test_a_sql_source_that_works_probes_as_ok(db, customer_db):
    assert DataSource(name="wms", url=customer_db).probe()["ok"] is True
