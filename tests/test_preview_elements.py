"""Every element kind, drawn. The preview is meant to be what the printer
does, so each kind needs to have been through it at least once."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app import preview
from app.models import Template
from tests.conftest import small_png

ROW = {"code": "7MB9042198", "logo": base64.b64encode(small_png()).decode()}


def _draw(element: dict) -> Image.Image:
    t = Template.model_validate({
        "id": "t", "name": "T", "width_mm": 60, "height_mm": 40, "dpi": 203,
        "elements": [element],
    })
    return Image.open(io.BytesIO(preview.render_png(t, ROW))).convert("L")


def _ink(img: Image.Image, box) -> bool:
    return img.crop(box).getextrema()[0] < 128


def test_a_qr_code_is_drawn(seeded):
    img = _draw({"kind": "qr", "name": "q", "value": "{{ code }}",
                 "box": {"x": 5, "y": 5, "w": 20, "h": 20}})

    x, y, side = 40, 40, 160          # 5 mm and 20 mm at 203 dpi
    assert _ink(img, (x, y, x + side, y + side)), "nothing was drawn"
    assert not _ink(img, (x + side + 10, y, x + side + 40, y + side)), "it ran over"


def test_a_box_is_an_outline_until_you_fill_it():
    hollow = _draw({"kind": "box", "name": "b", "thickness_mm": 0.5,
                    "box": {"x": 5, "y": 5, "w": 30, "h": 20}})
    filled = _draw({"kind": "box", "name": "b", "thickness_mm": 0.5, "fill": True,
                    "box": {"x": 5, "y": 5, "w": 30, "h": 20}})

    middle = (120, 100, 160, 130)     # well inside the box
    assert not _ink(hollow, middle), "a hollow box was filled in"
    assert _ink(filled, middle), "a filled box was not"
    assert _ink(hollow, (40, 40, 48, 48)), "the outline is missing"


def test_a_line_is_drawn_even_when_it_has_no_height():
    img = _draw({"kind": "line", "name": "l", "thickness_mm": 0.5,
                 "box": {"x": 5, "y": 10, "w": 40, "h": 0}})

    assert _ink(img, (40, 78, 360, 86))


def test_an_image_is_drawn_where_it_was_put():
    img = _draw({"kind": "image", "name": "m", "value": "{{ logo }}",
                 "box": {"x": 5, "y": 5, "w": 20, "h": 12}})

    assert _ink(img, (40, 40, 110, 80))


def test_a_hidden_element_is_not_drawn():
    img = _draw({"kind": "box", "name": "b", "hidden": True, "fill": True,
                 "box": {"x": 5, "y": 5, "w": 30, "h": 20}})

    assert img.getextrema() == (255, 255), "something was drawn anyway"


def test_a_binding_with_no_column_names_the_element():
    with pytest.raises(preview.RenderError) as exc:
        _draw({"kind": "text", "name": "consignee", "value": "{{ nope }}",
               "box": {"x": 5, "y": 5, "w": 40, "h": 8}})

    assert "consignee" in str(exc.value)
    assert "nope" in str(exc.value)


def _image_row(value, on_missing: str = "blank"):
    t = Template.model_validate({
        "id": "t", "name": "T", "width_mm": 60, "height_mm": 40, "dpi": 203,
        "elements": [{"kind": "image", "name": "mark", "value": "{{ junk }}",
                      "on_missing": on_missing,
                      "box": {"x": 5, "y": 5, "w": 20, "h": 12}}],
    })
    return Image.open(io.BytesIO(preview.render_png(t, {"junk": value}))).convert("L")


def test_an_unreadable_image_draws_nothing_because_that_is_what_prints():
    """The printer skips it and warns. A preview that draws a placeholder box
    would promise something that is not going to come out."""
    img = _image_row("not a picture")

    assert img.getextrema() == (255, 255)


def test_a_missing_image_draws_the_placeholder_when_that_is_what_was_asked_for():
    """^GB is what the printer puts there, so the preview draws the outline."""
    img = _image_row("", on_missing="placeholder")

    assert _ink(img, (40, 40, 50, 50)), "no outline"
    assert not _ink(img, (120, 80, 140, 90)), "the placeholder was filled in"


def test_a_missing_image_draws_nothing_when_it_was_told_to_blank():
    assert _image_row("", on_missing="blank").getextrema() == (255, 255)


def test_on_missing_fail_fails_the_preview_too():
    """Otherwise a good-looking preview is followed by a failed run."""
    with pytest.raises(preview.RenderError) as exc:
        _image_row("not a picture", on_missing="fail")
    assert "mark" in str(exc.value)

    with pytest.raises(preview.RenderError):
        _image_row("", on_missing="fail")
