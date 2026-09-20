"""Connections to the customer's own databases, and the saved queries that run
against them. Everything here is read-only by construction.

The records live in the datasource / saved_query / query_parameter tables;
the engines (connection pools) live here in memory, one per datasource, and
are rebuilt if the stored URL changes."""

from __future__ import annotations

import base64
import binascii
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from db import models as db

from . import images

PLACEHOLDER = re.compile(r":([a-zA-Z_]\w*)")
REST_TIMEOUT = 20.0


@dataclass
class DataSource:
    name: str
    url: str                       # postgresql+psycopg://platen_ro@host/db, or a base URL
    label: str = ""
    kind: str = "sql"              # sql | rest
    headers: dict[str, str] = field(default_factory=dict)
    pool_size: int = 5
    _engine: Engine | None = field(default=None, repr=False)

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(
                self.url,
                pool_size=self.pool_size,
                pool_pre_ping=True,
                connect_args={"options": "-c default_transaction_read_only=on"}
                if self.url.startswith("postgresql")
                else {},
            )
        return self._engine

    def probe(self) -> dict[str, Any]:
        if self.kind == "rest":
            r = httpx.get(self.url, headers=self.headers, timeout=REST_TIMEOUT)
            return {"ok": r.status_code < 500, "status": r.status_code,
                    "url": self.url.split("@")[-1]}
        with self.engine.connect() as c:
            c.execute(text("select 1"))
        return {"ok": True, "url": self.url.split("@")[-1]}


@dataclass
class SavedQuery:
    name: str
    datasource: str
    sql: str                       # uses :named parameters, never f-strings
    row_path: str = ""             # rest only: where the rows sit in the response
    parameters: list[Parameter] = field(default_factory=list)


@dataclass
class Parameter:
    name: str
    type: str = "text"             # text | int | date | bool
    default: Any = None
    ask_at_print: bool = True
    label: str = ""


MASK = "***"                       # what SQLAlchemy renders a hidden password as


def masked(url: str) -> str:
    """A connection string safe to put in a browser. The password never leaves
    the server, so an operator with the screen open can't read it off it."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return url


def masked_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Header names are useful to see; their values are tokens."""
    return dict.fromkeys(headers or {}, MASK)


def unmasked_headers(submitted: dict[str, str] | None,
                     stored: dict[str, str] | None) -> dict[str, str]:
    stored = stored or {}
    return {k: (stored.get(k, "") if v == MASK else v)
            for k, v in (submitted or {}).items()}


def unmasked(submitted: str, stored: str | None) -> str:
    """The other half: a URL saved back untouched keeps the password it had."""
    if stored is None:
        return submitted
    try:
        new, old = make_url(submitted), make_url(stored)
    except Exception:
        return submitted
    if new.password != MASK:
        return submitted
    return new.set(password=old.password).render_as_string(hide_password=False)


_ENGINES: dict[str, DataSource] = {}


def datasource(session: Session, datasource_id: str) -> DataSource | None:
    """The DataSource for a row, with its engine cached across requests."""
    row = session.get(db.DataSource, datasource_id)
    if row is None:
        return None
    if row.kind == "rest":
        return DataSource(name=row.id, url=row.url, label=row.label or row.name,
                          kind="rest", headers=dict(row.headers or {}))
    cached = _ENGINES.get(row.id)
    if cached is None or cached.url != row.url or cached.pool_size != row.pool_size:
        if cached is not None and cached._engine is not None:
            cached._engine.dispose()
        cached = DataSource(name=row.id, url=row.url, label=row.label or row.name,
                            pool_size=row.pool_size)
        _ENGINES[row.id] = cached
    return cached


def saved_query(session: Session, query_id: str) -> SavedQuery | None:
    row = session.get(db.SavedQuery, query_id)
    if row is None:
        return None
    return SavedQuery(
        name=row.id, datasource=row.datasource_id, sql=row.sql, row_path=row.row_path or "",
        parameters=[Parameter(name=p.name, type=p.type, default=p.default,
                              ask_at_print=p.ask_at_print, label=p.label)
                    for p in row.parameters],
    )


def run(session: Session, query: SavedQuery, params: dict[str, Any],
        limit: int | None = None) -> list[dict[str, Any]]:
    """Execute a saved query as a prepared statement and return plain dicts."""
    ds = datasource(session, query.datasource)
    if ds is None:
        raise ValueError(
            f"query {query.name!r}: its data source {query.datasource!r} no longer exists")
    bound = {p.name: params.get(p.name, p.default) for p in query.parameters}
    missing = [p.name for p in query.parameters if bound[p.name] is None]
    if missing:
        raise ValueError(f"missing parameters: {', '.join(missing)}")

    if ds.kind == "rest":
        return _fetch(ds, query, bound, limit)

    sql = query.sql if limit is None else f"select * from ({query.sql}) q limit {int(limit)}"
    with ds.engine.connect() as c:
        result = c.execute(text(sql), bound)
        return [dict(r) for r in result.mappings()]


def _fetch(ds: DataSource, query: SavedQuery, bound: dict[str, Any],
           limit: int | None) -> list[dict[str, Any]]:
    """One GET, and only ever a GET. An operator's answer is encoded into the
    request — quoted in the path, or sent as a query parameter — never pasted
    in, for the same reason it is never pasted into SQL."""
    used: set[str] = set()

    def place(m: re.Match[str]) -> str:
        used.add(m.group(1))
        return urllib.parse.quote(str(bound.get(m.group(1), "")), safe="")

    path = PLACEHOLDER.sub(place, query.sql.strip())
    extra = {k: v for k, v in bound.items() if k not in used and v is not None}
    url = f"{ds.url.rstrip('/')}/{path.lstrip('/')}" if path else ds.url

    try:
        response = httpx.get(url, params=extra, headers=ds.headers,
                             timeout=REST_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"{query.name}: the endpoint answered {exc.response.status_code}"
        ) from None
    except httpx.HTTPError as exc:
        raise ValueError(f"{query.name}: could not reach the endpoint — {exc}") from None
    except ValueError:
        raise ValueError(f"{query.name}: the endpoint did not answer with JSON") from None

    rows = _dig(body, query.row_path, query.name)
    return rows if limit is None else rows[:limit]


def _dig(body: Any, path: str, query_name: str) -> list[dict[str, Any]]:
    node = body
    for part in [p for p in path.split(".") if p]:
        if not isinstance(node, dict) or part not in node:
            raise ValueError(
                f"{query_name}: the response has nothing at {path!r}"
            )
        node = node[part]
    if not isinstance(node, list) or not all(isinstance(r, dict) for r in node):
        where = path or "the top level of the response"
        raise ValueError(f"{query_name}: {where} is not a list of records")
    return node


def columns(session: Session, query: SavedQuery) -> list[str]:
    """What a query returns, without needing its parameters answered.

    Every parameter binds as NULL and the statement is limited to no rows, so
    the database still describes the result it would have produced. The editor
    uses this to know which bindings can resolve before anyone has typed a
    despatch date.
    """
    ds = datasource(session, query.datasource)
    if ds is None:
        raise ValueError(
            f"query {query.name!r}: its data source {query.datasource!r} no longer exists")
    bound: dict[str, Any] = {p.name: None for p in query.parameters}
    if ds.kind == "rest":
        # no endpoint can describe its own shape, so ask for one record
        rows = _fetch(ds, query, bound, limit=1)
        return list(rows[0]) if rows else []
    with ds.engine.connect() as c:
        return list(c.execute(text(f"select * from ({query.sql}) q limit 0"), bound).keys())


def describe(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What the editor shows as the field list — and which columns are images.

    This is the auto-detection behind the "base64 PNG" badge in the UI.
    """
    if not rows:
        return []
    out = []
    for key, sample in rows[0].items():
        out.append({"name": key, "type": _classify(key, sample)})
    return out


def _classify(name: str, sample: Any) -> str:
    if isinstance(sample, (bytes, memoryview)) and images.sniff(bytes(sample)[:8]):
        return "image"
    if isinstance(sample, str) and len(sample) > 64:
        try:
            if images.sniff(base64.b64decode("".join(sample.split())[:64], validate=False)):
                return "base64_image"
        except (binascii.Error, ValueError):
            pass
    if isinstance(sample, bool):
        return "bool"
    if isinstance(sample, int):
        return "int"
    if isinstance(sample, float):
        return "numeric"
    return type(sample).__name__ if sample is not None else "text"
