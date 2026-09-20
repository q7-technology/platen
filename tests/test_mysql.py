"""Platen offers MySQL. This is the only thing that has ever connected to one.

Skipped unless PLATEN_TEST_MYSQL_URL is set; CI sets it against a service
container. Run it here with:

    docker run -d --rm --name mysql -e MYSQL_ROOT_PASSWORD=root \\
        -e MYSQL_DATABASE=wms -p 13306:3306 mysql:8
    PLATEN_TEST_MYSQL_URL=mysql+pymysql://root:root@127.0.0.1:13306/wms pytest tests/test_mysql.py
"""

from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import datasources
from app.datasources import DataSource, Parameter, SavedQuery

URL = os.environ.get("PLATEN_TEST_MYSQL_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="no MySQL to talk to")


class OneSource:
    """Stands in for the session lookup: one data source row, by any id."""

    def __init__(self, url: str) -> None:
        self.row = SimpleNamespace(id="wms", name="wms", label="", kind="sql",
                                   url=url, headers={}, pool_size=5)

    def get(self, model, key):
        return self.row


@pytest.fixture
def mysql():
    source = DataSource(name="wms", url=URL)
    with source.engine.begin() as c:
        c.execute(text("drop table if exists platen_cartons"))
        c.execute(text("create table platen_cartons ("
                       "consignment_no varchar(32), consignee_name varchar(64), "
                       "despatch_date date, net_kg decimal(6,2))"))
        c.execute(text("insert into platen_cartons values "
                       "('7MB9042101','Coles DC Truganina','2026-09-18',18.40),"
                       "('7MB9042102','IGA Ballarat','2026-09-18',9.20),"
                       "('7MB9042103','Aldi DC Dandenong','2026-09-19',12.00)"))
    datasources._ENGINES["wms"] = source
    yield source
    with source.engine.begin() as c:
        c.execute(text("drop table if exists platen_cartons"))


@pytest.fixture
def query(mysql):
    return SavedQuery(
        name="cartons", datasource="wms",
        sql="select * from platen_cartons where despatch_date = :d order by consignment_no",
        parameters=[Parameter(name="d", type="date")],
    )


def _session(mysql):
    return OneSource(URL)


def test_a_parameter_is_bound_not_pasted(mysql, query):
    rows = datasources.run(_session(mysql), query, {"d": "2026-09-18"})

    assert [r["consignment_no"] for r in rows] == ["7MB9042101", "7MB9042102"]


def test_a_limit_works_on_mysqls_own_syntax(mysql, query):
    assert len(datasources.run(_session(mysql), query, {"d": "2026-09-18"}, limit=1)) == 1


def test_the_columns_come_back_without_answering_the_parameter(mysql, query):
    assert datasources.columns(_session(mysql), query) == [
        "consignment_no", "consignee_name", "despatch_date", "net_kg"]


def test_mysql_types_survive_the_trip(mysql, query):
    from decimal import Decimal

    row = datasources.run(_session(mysql), query, {"d": "2026-09-18"})[0]

    assert isinstance(row["despatch_date"], date)
    assert isinstance(row["net_kg"], Decimal)


def test_those_types_render_onto_a_label(mysql, query):
    from app.binding import render_value

    row = datasources.run(_session(mysql), query, {"d": "2026-09-18"})[0]

    assert render_value("{{ despatch_date | date:%d/%m/%y }}", row) == "18/09/26"
    assert render_value("{{ net_kg | round:1 }} kg", row) == "18.4 kg"
    assert render_value("{{ consignee_name | upper }}", row) == "COLES DC TRUGANINA"
