"""Turning "{{ shipment.sku }}" into a value from a row.

Deliberately dumb: dotted lookups and nothing else. No expressions, no calls,
no eval — a template comes from the browser and must never be able to run code.
Formatting happens through a small named-filter list instead.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping

BINDING = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)((?:\s*\|\s*\w+(?::[^|}]+)?)*)\s*\}\}")


class MissingField(KeyError):
    """A template asks for a column the query did not return."""


def _lookup(path: str, row: Mapping[str, Any]) -> Any:
    node: Any = row
    for part in path.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, Mapping):
            # rows come back flat, so "shipment.sku" also matches the column "sku"
            leaf = path.split(".")[-1]
            if leaf in node:
                return node[leaf]
            raise MissingField(path)
        else:
            node = getattr(node, part, None)
    return node


def _apply(value: Any, spec: str) -> Any:
    name, _, arg = spec.partition(":")
    name = name.strip()
    arg = arg.strip()
    if name == "upper":
        return str(value).upper()
    if name == "date":
        if isinstance(value, (date, datetime)):
            return value.strftime(arg or "%d/%m/%y")
        return value
    if name == "round":
        return f"{Decimal(str(value)):.{int(arg or 2)}f}"
    if name == "pad":
        return str(value).rjust(int(arg or 0), "0")
    if name == "default":
        return arg if value in (None, "") else value
    raise ValueError(f"unknown filter: {name}")


def render_value(expr: str, row: Mapping[str, Any]) -> str:
    """Substitute every binding in `expr`. Literal text passes straight through."""

    def sub(m: re.Match[str]) -> str:
        value = _lookup(m.group(1), row)
        for spec in (s for s in m.group(2).split("|") if s.strip()):
            value = _apply(value, spec)
        return "" if value is None else str(value)

    return BINDING.sub(sub, expr)


def raw_value(expr: str, row: Mapping[str, Any]) -> Any:
    """For elements that want the value itself (an image blob), not its text."""
    m = BINDING.fullmatch(expr.strip())
    return _lookup(m.group(1), row) if m else expr


def fields_used(expr: str) -> set[str]:
    """What the editor highlights in the field list."""
    return {m.group(1) for m in BINDING.finditer(expr)}
