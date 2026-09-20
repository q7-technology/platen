"""The preview has one job: look like what the printer will do."""

from __future__ import annotations

import io

from PIL import Image

from app import preview
from app.models import Template

ROW = {"consignment_no": "7MB9042198"}


def _png(human_readable: str) -> Image.Image:
    t = Template.model_validate({
        "id": "t", "name": "T", "width_mm": 40, "height_mm": 30, "dpi": 203,
        "elements": [{
            "kind": "barcode", "name": "code", "value": "{{ consignment_no }}",
            "box": {"x": 5, "y": 5, "w": 30, "h": 10},
            "human_readable": human_readable, "module_dots": 2,
        }],
    })
    return Image.open(io.BytesIO(preview.render_png(t, ROW))).convert("L")


def _has_ink(img: Image.Image, box: tuple[int, int, int, int]) -> bool:
    return img.crop(box).getextrema()[0] < 128


def test_bars_fill_the_element_box_the_way_zpl_draws_them():
    """^BC's height is the bars alone, so the bars own the whole box."""
    img = _png("none")
    x, y, w, h = 40, 40, 240, 80                      # 5mm, 5mm, 30mm, 10mm at 203 dpi

    assert _has_ink(img, (x, y, x + w, y + 4)), "no bars at the top of the box"
    assert _has_ink(img, (x, y + h - 4, x + w, y + h)), "bars stop short of the bottom"


def test_the_readable_line_sits_below_the_bars_not_inside_them():
    """^BC prints the interpretation line under the bar height, not within it."""
    x, y, w, h = 40, 40, 240, 80

    bars_only = _png("none").crop((x, y, x + w, y + h))
    with_text = _png("below").crop((x, y, x + w, y + h))

    assert with_text.tobytes() == bars_only.tobytes(), "the readable line ate into the bars"
    assert _has_ink(_png("below"), (x, y + h, x + w, y + h + 24)), "no readable line below the bars"
