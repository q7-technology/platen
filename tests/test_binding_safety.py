"""A template arrives from a browser and gets printed. It must not be able to
reach anything but the data in the row.

Invariant 1: dotted lookups and a fixed list of filters. No attribute
traversal into callables, and nothing that starts with an underscore.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.binding import BINDING, MissingField, fields_used, render_value


class Order:
    """What an ORM row or a decoded REST object looks like."""

    def __init__(self) -> None:
        self.sku = "BF-2201"
        self.qty = 24
        self._api_key = "sh_live_secret"

    def despatch(self) -> str:
        return "sent"


ROW = {"order": Order(), "when": date(2026, 9, 18), "sku": "BF-2201", "_internal": "fine"}


# ------------------------------------------------------------- what is allowed

def test_a_plain_column_resolves():
    assert render_value("{{ sku }}", ROW) == "BF-2201"


def test_a_dotted_path_reaches_a_value_on_an_object():
    assert render_value("{{ order.sku }}", ROW) == "BF-2201"
    assert render_value("{{ order.qty }}", ROW) == "24"


def test_a_value_on_a_date_still_works():
    assert render_value("{{ when.year }}", ROW) == "2026"


def test_a_column_of_that_name_wins_over_a_path_that_does_not_exist():
    """Rows come back flat, so shipment.sku matches the column sku."""
    assert render_value("{{ shipment.sku }}", ROW) == "BF-2201"


def test_an_underscore_column_is_still_a_column():
    """The rule is about attributes, not about what a DBA named a field."""
    assert render_value("{{ _internal }}", ROW) == "fine"


# ------------------------------------------------------------- what is refused

@pytest.mark.parametrize("expr", [
    "{{ order.__class__ }}",
    "{{ order.__dict__ }}",
    "{{ order.__init__ }}",
    "{{ order.__init__.__globals__ }}",
    "{{ order.__class__.__mro__ }}",
    "{{ when.__class__.__base__ }}",
])
def test_a_template_cannot_walk_into_python_itself(expr):
    with pytest.raises(MissingField):
        render_value(expr, ROW)


def test_a_private_attribute_is_not_data():
    with pytest.raises(MissingField):
        render_value("{{ order._api_key }}", ROW)


def test_a_method_is_never_the_answer():
    """Reaching a callable is how you end up one step from calling it."""
    with pytest.raises(MissingField):
        render_value("{{ order.despatch }}", ROW)


def test_nothing_leaks_through_a_filter_either():
    with pytest.raises(MissingField):
        render_value("{{ order.__class__ | upper }}", ROW)


def test_a_module_cannot_be_reached_through_a_value():
    import sys
    with pytest.raises(MissingField):
        render_value("{{ mod.path }}", {"mod": sys})


# ------------------------------------------------------------------- filters

def test_upper_shouts():
    assert render_value("{{ sku | upper }}", ROW) == "BF-2201"
    assert render_value("{{ name | upper }}", {"name": "coles dc"}) == "COLES DC"


def test_date_formats_a_real_date_and_leaves_anything_else_alone():
    assert render_value("{{ when | date:%d/%m/%y }}", ROW) == "18/09/26"
    assert render_value("{{ when | date }}", ROW) == "18/09/26"
    assert render_value("{{ sku | date }}", ROW) == "BF-2201"


def test_pad_makes_a_fixed_width_number():
    assert render_value("{{ n | pad:6 }}", {"n": 42}) == "000042"


def test_default_fills_a_hole():
    assert render_value("{{ note | default:none given }}", {"note": None}) == "none given"
    assert render_value("{{ note | default:x }}", {"note": "real"}) == "real"


def test_the_editor_can_see_which_fields_an_expression_uses():
    assert fields_used("{{ a }} and {{ b.c | upper }}") == {"a", "b.c"}
    assert fields_used("no bindings here") == set()


def test_the_pattern_does_not_match_something_that_is_not_a_binding():
    assert BINDING.search("{{ 1 + 1 }}") is None
    assert BINDING.search("{{ open('x') }}") is None
