"""Data Matrix, which the landing page has been promising all along."""

from __future__ import annotations

import io

from PIL import Image
from ppf.datamatrix import DataMatrix

from app import preview, zplimport
from app.models import DataMatrixElement, Template
from app.zpl import ZplRenderer

ROW = {"batch": "B26-2551"}


def _template(**overrides) -> Template:
    element = {
        "kind": "datamatrix", "name": "mark", "value": "{{ batch }}",
        "box": {"x": 5, "y": 5, "w": 20, "h": 20}, "module_dots": 6,
    } | overrides
    return Template.model_validate({
        "id": "t", "name": "T", "width_mm": 50, "height_mm": 40, "dpi": 203,
        "elements": [element],
    })


def test_a_data_matrix_element_exists_at_all():
    element = _template().elements[0]

    assert isinstance(element, DataMatrixElement)
    assert element.quality == 200                     # ECC200, the only one worth using


def test_it_renders_the_printer_its_own_command():
    """^BX carries the data; the print head does the encoding."""
    zpl = ZplRenderer(_template()).render(ROW).zpl

    assert "^BXN,6,200" in zpl
    assert "^FDB26-2551^FS" in zpl


def test_rotation_reaches_the_command():
    zpl = ZplRenderer(_template(box={"x": 5, "y": 5, "w": 20, "h": 20, "rotation": 90})).render(ROW).zpl

    assert "^BXR,6,200" in zpl


def test_the_preview_draws_the_same_matrix_the_printer_will():
    """The preview has to agree with the print head, so it encodes ECC200 too
    rather than drawing a square of noise."""
    expected = DataMatrix("B26-2551").matrix
    modules = len(expected)

    image = Image.open(io.BytesIO(preview.render_png(_template(), ROW))).convert("L")

    # the symbol starts at 5 mm and each module is 6 dots square
    x0, y0 = round(5 * 203 / 25.4), round(5 * 203 / 25.4)
    for row in (0, modules // 2, modules - 1):
        for col in (0, modules // 2, modules - 1):
            px = image.getpixel((x0 + col * 6 + 3, y0 + row * 6 + 3))
            assert (px < 128) == bool(expected[row][col]), f"module {row},{col} is wrong"


def test_a_blank_value_does_not_draw_a_stray_square():
    zpl = ZplRenderer(_template(value="{{ missing | default: }}")).render({"missing": ""}).zpl

    assert "^BX" not in zpl


def test_importing_bx_brings_it_back():
    zpl = "^XA^PW406^LL324^FO40,40^BXN,6,200^FDB26-2551^FS^XZ"

    result = zplimport.parse(zpl, id="i", name="I", dpi=203)

    element = result.template.elements[0]
    assert isinstance(element, DataMatrixElement)
    assert element.value == "B26-2551"
    assert element.module_dots == 6
    assert element.quality == 200


def test_a_template_with_a_data_matrix_survives_a_round_trip():
    original = _template()
    zpl = ZplRenderer(original).render(ROW).zpl

    back = zplimport.parse(zpl, id="i", name="I", dpi=203).template.elements[0]

    assert back.kind == "datamatrix"
    assert back.module_dots == original.elements[0].module_dots
    assert round(back.box.x) == 5
