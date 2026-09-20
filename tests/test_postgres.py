"""Postgres as a data source, which is what most sites will point Platen at.

Skipped unless PLATEN_TEST_POSTGRES_URL is set to an account that can create a
table and a role; CI sets it against a service container. Run it here with:

    docker run -d --rm --name pg -e POSTGRES_USER=platen -e POSTGRES_PASSWORD=platen \\
        -e POSTGRES_DB=platen -p 15432:5432 postgres:16
    PLATEN_TEST_POSTGRES_URL=postgresql+psycopg://platen:platen@127.0.0.1:15432/platen \\
        pytest tests/test_postgres.py
"""

from __future__ import annotations

import io
import os
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import datasources, images
from app.binding import render_value
from app.datasources import Parameter, SavedQuery

ADMIN = os.environ.get("PLATEN_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not ADMIN, reason="no Postgres to talk to")

TABLE = "platen_cartons"
RO_USER, RO_PASSWORD = "platen_ro", "ro-for-tests"


def _readonly_url() -> str:
    """The URL a site would actually configure: the read-only role."""
    return make_url(ADMIN).set(username=RO_USER, password=RO_PASSWORD).render_as_string(
        hide_password=False)


class OneSource:
    """Stands in for the session lookup: one data source row, by any id."""

    def __init__(self, url: str) -> None:
        self.row = SimpleNamespace(id="wms", name="wms", label="", kind="sql",
                                   url=url, headers={}, pool_size=5)

    def get(self, model, key):
        return self.row


def _mark() -> bytes:
    img = Image.new("L", (64, 40), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([4, 4, 28, 36], fill=0)
    draw.ellipse([34, 6, 60, 34], outline=0, width=4)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def warehouse():
    """A table and the read-only role the README tells people to make.

    Set up through a plain engine, not Platen's: every Postgres connection
    Platen opens is read-only, which is the point of the test below.
    """
    admin = create_engine(ADMIN)
    with admin.begin() as c:
        c.execute(text(f"drop table if exists {TABLE}"))
        c.execute(text(f"create table {TABLE} (consignment_no text, consignee_name text, "
                       "despatch_date date, net_kg numeric(6,2), mark bytea)"))
        c.execute(text(f"insert into {TABLE} values "
                       "('7MB9042101','Coles DC Truganina','2026-09-18',18.40,:mark),"
                       "('7MB9042102','IGA Ballarat','2026-09-18',9.20,null),"
                       "('7MB9042103','Aldi DC Dandenong','2026-09-19',12.00,null)"),
                  {"mark": _mark()})
        c.execute(text(f"drop role if exists {RO_USER}"))
        c.execute(text(f"create role {RO_USER} login password '{RO_PASSWORD}'"))
        c.execute(text("grant usage on schema public to " + RO_USER))
        c.execute(text(f"grant select on {TABLE} to {RO_USER}"))

    datasources._ENGINES.clear()
    yield OneSource(_readonly_url())

    with admin.begin() as c:
        c.execute(text(f"revoke all on {TABLE} from {RO_USER}"))
        c.execute(text("revoke usage on schema public from " + RO_USER))
        c.execute(text(f"drop table if exists {TABLE}"))
        c.execute(text(f"drop role if exists {RO_USER}"))
    admin.dispose()
    datasources._ENGINES.clear()


@pytest.fixture
def query():
    return SavedQuery(
        name="cartons", datasource="wms",
        sql=f"select * from {TABLE} where despatch_date = :d order by consignment_no",
        parameters=[Parameter(name="d", type="date")],
    )


# ------------------------------------------------------------------ the basics

def test_it_connects(warehouse):
    assert datasources.datasource(warehouse, "wms").probe()["ok"] is True


def test_a_parameter_is_bound_not_pasted(warehouse, query):
    rows = datasources.run(warehouse, query, {"d": "2026-09-18"})

    assert [r["consignment_no"] for r in rows] == ["7MB9042101", "7MB9042102"]


def test_a_limit_works_on_postgres_syntax(warehouse, query):
    assert len(datasources.run(warehouse, query, {"d": "2026-09-18"}, limit=1)) == 1


def test_the_columns_come_back_without_answering_the_parameter(warehouse, query):
    assert datasources.columns(warehouse, query) == [
        "consignment_no", "consignee_name", "despatch_date", "net_kg", "mark"]


# -------------------------------------------------------------- read-only

def test_a_write_is_refused_by_the_connection_itself(warehouse):
    """Platen opens Postgres with default_transaction_read_only=on, so a write
    is refused before role permissions even come into it. Invariant 2."""
    write = SavedQuery(name="naughty", datasource="wms",
                       sql=f"insert into {TABLE} values ('X','X','2026-09-18',1,null)")

    with pytest.raises(Exception) as exc:
        datasources.run(warehouse, write, {})

    assert "read-only" in str(exc.value).lower()


def test_so_is_anything_else_that_changes_the_place(warehouse):
    for sql in (f"update {TABLE} set consignee_name = 'nope'",
                f"delete from {TABLE}",
                f"drop table {TABLE}"):
        with pytest.raises(Exception) as exc:
            datasources.run(warehouse, SavedQuery(name="bad", datasource="wms", sql=sql), {})
        assert "read-only" in str(exc.value).lower(), sql


# ----------------------------------------------------------------- the types

def test_postgres_types_survive_the_trip(warehouse, query):
    row = datasources.run(warehouse, query, {"d": "2026-09-18"})[0]

    assert isinstance(row["despatch_date"], date)
    assert isinstance(row["net_kg"], Decimal)
    assert isinstance(row["mark"], (bytes, memoryview))


def test_those_types_render_onto_a_label(warehouse, query):
    row = datasources.run(warehouse, query, {"d": "2026-09-18"})[0]

    assert render_value("{{ despatch_date | date:%d/%m/%y }}", row) == "18/09/26"
    assert render_value("{{ net_kg | round:1 }} kg", row) == "18.4 kg"
    assert render_value("{{ consignee_name | upper }}", row) == "COLES DC TRUGANINA"


# -------------------------------------------------------------------- images

def test_a_bytea_column_is_recognised_as_an_image(warehouse, query):
    rows = datasources.run(warehouse, query, {"d": "2026-09-18"})

    kinds = {f["name"]: f["type"] for f in datasources.describe(rows)}
    assert kinds["mark"] == "image"


def test_an_encoded_column_is_recognised_too(warehouse):
    rows = datasources.run(warehouse, SavedQuery(
        name="encoded", datasource="wms",
        sql=f"select encode(mark,'base64') as mark_b64 from {TABLE} "
            "where consignment_no = '7MB9042101'"), {})

    kinds = {f["name"]: f["type"] for f in datasources.describe(rows)}
    assert kinds["mark_b64"] == "base64_image"


def test_bytea_and_base64_make_the_same_dots(warehouse):
    """Nobody should have to remember to wrap it in encode()."""
    row = datasources.run(warehouse, SavedQuery(
        name="both", datasource="wms",
        sql=f"select mark, encode(mark,'base64') as mark_b64 from {TABLE} "
            "where consignment_no = '7MB9042101'"), {})[0]

    direct = images.to_graphic(row["mark"], 64, 40, dither=False)
    encoded = images.to_graphic(row["mark_b64"], 64, 40, dither=False)

    assert direct.data == encoded.data
    assert (direct.width_dots, direct.height_dots) == (64, 40)


def test_a_row_with_no_image_does_not_stop_the_run(warehouse, query):
    rows = datasources.run(warehouse, query, {"d": "2026-09-18"})

    assert rows[1]["mark"] is None
