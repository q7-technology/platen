"""Turning "{{ shipment.sku }}" into a value from a row.

Deliberately dumb: dotted lookups and nothing else. No expressions, no calls,
no eval — a template comes from the browser and must never be able to run code.
Formatting happens through a small named-filter list instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import ModuleType
from typing import Any

BINDING = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)((?:\s*\|\s*\w+(?::[^|}]+)?)*)\s*\}\}")


class BindingError(Exception):
    """A binding could not produce a value. Always names what and why."""


class MissingField(BindingError, KeyError):
    """A template asks for a column the query did not return."""


class FilterError(BindingError, ValueError):
    """A named filter was handed something it can't work with."""


class Placeholder(str):
    """A column standing in for its own value in a layout preview.

    Filters leave it alone: a rounding or a date format can't apply to a column
    name, and failing there would report a fault the template doesn't have.
    """


_MISSING = object()


def _attribute(node: Any, part: str, path: str) -> Any:
    """One step into an object, and only ever into data.

    An attribute step is the one place a binding could walk out of the row and
    into Python itself. A name starting with an underscore is refused, which
    stops every dunder in one rule; so is anything that comes back callable,
    because reaching a callable is a step away from calling it; and so is a
    module or a class, which is never something a query returned.
    """
    if part.startswith("_") or isinstance(node, (ModuleType, type)):
        raise MissingField(path)
    value = getattr(node, part, _MISSING)
    if value is _MISSING or callable(value) or isinstance(value, ModuleType):
        raise MissingField(path)
    return value


def _lookup(path: str, row: Mapping[str, Any]) -> Any:
    node: Any = row
    for part in path.split("."):
        if isinstance(node, Mapping):
            if part in node:
                node = node[part]
                continue
            # rows come back flat, so "shipment.sku" also matches the column "sku"
            leaf = path.split(".")[-1]
            if leaf in node:
                return node[leaf]
            raise MissingField(path)
        node = _attribute(node, part, path)
    return node


def _apply(value: Any, spec: str) -> Any:
    name, _, arg = spec.partition(":")
    name = name.strip()
    arg = arg.strip()
    if name not in FILTERS:
        raise FilterError(f"unknown filter {name!r}; one of {', '.join(sorted(FILTERS))}")
    if isinstance(value, Placeholder):
        return value
    return FILTERS[name](value, arg)


def _upper(value: Any, arg: str) -> str:
    return str(value).upper()


def _date(value: Any, arg: str) -> Any:
    if isinstance(value, (date, datetime)):
        return value.strftime(arg or "%d/%m/%y")
    return value


def _round(value: Any, arg: str) -> str:
    try:
        places = int(arg or 2)
    except ValueError:
        raise FilterError(f"round wants a number of places, not {arg!r}") from None
    try:
        return f"{Decimal(str(value)):.{places}f}"
    except (InvalidOperation, ValueError):
        raise FilterError(f"round expects a number, and this value is {value!r}") from None


def _pad(value: Any, arg: str) -> str:
    try:
        width = int(arg or 0)
    except ValueError:
        raise FilterError(f"pad wants a width, not {arg!r}") from None
    return str(value).rjust(width, "0")


def _default(value: Any, arg: str) -> Any:
    return arg if value in (None, "") else value


FILTERS = {"upper": _upper, "date": _date, "round": _round, "pad": _pad, "default": _default}


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
