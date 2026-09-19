"""Connections to the customer's own databases, and the saved queries that run
against them. Everything here is read-only by construction."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from . import images


@dataclass
class DataSource:
    name: str
    url: str                       # postgresql+psycopg://platen_ro@host/db
    label: str = ""
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
        with self.engine.connect() as c:
            c.execute(text("select 1"))
        return {"ok": True, "url": self.url.split("@")[-1]}


@dataclass
class SavedQuery:
    name: str
    datasource: str
    sql: str                       # uses :named parameters, never f-strings
    parameters: list["Parameter"] = field(default_factory=list)


@dataclass
class Parameter:
    name: str
    type: str = "text"             # text | int | date | bool
    default: Any = None
    ask_at_print: bool = True
    label: str = ""


REGISTRY: dict[str, DataSource] = {}
QUERIES: dict[str, SavedQuery] = {}


def run(query: SavedQuery, params: dict[str, Any], limit: int | None = None):
    """Execute a saved query as a prepared statement and return plain dicts."""
    ds = REGISTRY[query.datasource]
    bound = {p.name: params.get(p.name, p.default) for p in query.parameters}
    missing = [p.name for p in query.parameters if bound[p.name] is None and not p.ask_at_print]
    if missing:
        raise ValueError(f"missing parameters: {', '.join(missing)}")

    sql = query.sql if limit is None else f"select * from ({query.sql}) q limit {int(limit)}"
    with ds.engine.connect() as c:
        result = c.execute(text(sql), bound)
        return [dict(r) for r in result.mappings()]


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
