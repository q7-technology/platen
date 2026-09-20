"""Reading somebody else's ZPL back into a template."""

from __future__ import annotations

import base64
import io

from PIL import Image

from app import images, zplimport
from app.models import BarcodeElement, BoxShape, ImageElement, LineShape, QrElement, TextElement
from tests.conftest import small_png

LABEL = """
^XA
^PW812^LL1218^LH0,0^MD12
^FO48,160^A0N,39,0^FDColes DC Truganina^FS
^FO48,330^A0N,32,0^FH_^FDCoopers _26 Sons^FS
^FO48,496^BY3,3,160^BCN,160,Y,N,N^FD7MB9042198^FS
^FO48,700^BQN,2,5^FDQA,https://q7.io/t/7MB^FS
^FO48,900^GB716,3,3^FS
^FO48,930^GB300,200,4^FS
^XZ
"""


def _parse(zpl: str = LABEL, dpi: int = 203):
    return zplimport.parse(zpl, id="imported", name="Imported", dpi=dpi)


def _by_kind(t, kind):
    return [e for e in t.elements if e.kind == kind]


def test_the_label_size_comes_back_in_millimetres():
    t = _parse().template

    assert t.dpi == 203
    assert round(t.width_mm, 1) == 101.6          # 812 dots at 203 dpi
    assert round(t.height_mm, 1) == 152.4
    assert t.darkness == 12


def test_a_text_field_keeps_its_place_and_its_size():
    text = _by_kind(_parse().template, "text")[0]

    assert isinstance(text, TextElement)
    assert text.value == "Coles DC Truganina"
    assert round(text.box.x, 1) == 6.0            # 48 dots
    assert round(text.box.y, 1) == 20.0           # 160 dots
    assert round(text.height_pt) == 14            # 39 dots at 203 dpi


def test_hex_escapes_are_decoded_back_to_the_character():
    """^FH means the data is hex-escaped; an importer that keeps the escape
    would print a literal underscore-two-six."""
    text = _by_kind(_parse().template, "text")[1]

    assert text.value == "Coopers & Sons"


def test_a_barcode_keeps_its_symbology_module_width_and_readable_line():
    bar = _by_kind(_parse().template, "barcode")[0]

    assert isinstance(bar, BarcodeElement)
    assert bar.value == "7MB9042198"
    assert bar.symbology == "code128"
    assert bar.module_dots == 3
    assert bar.human_readable == "below"
    assert round(bar.box.h, 1) == 20.0            # 160 dots


def test_a_qr_code_loses_its_error_correction_prefix():
    qr = _by_kind(_parse().template, "qr")[0]

    assert isinstance(qr, QrElement)
    assert qr.value == "https://q7.io/t/7MB"
    assert qr.error_correction == "Q"
    assert qr.magnification == 5


def test_a_thin_graphic_box_becomes_a_line_and_a_fat_one_a_box():
    t = _parse().template

    line = _by_kind(t, "line")[0]
    box = _by_kind(t, "box")[0]
    assert isinstance(line, LineShape) and isinstance(box, BoxShape)
    assert round(line.box.w, 1) == 89.6           # 716 dots
    assert round(box.box.w, 1) == 37.5            # 300 dots


def test_a_graphic_round_trips_back_to_the_same_bitmap():
    """^GFA in, the same dots out — the import is not a lossy redraw."""
    png = small_png()
    graphic = images.to_graphic(base64.b64encode(png).decode(), 40, 24, dither=False)
    zpl = f"^XA^PW812^LL1218^FO48,48{graphic.zpl}^FS^XZ"

    element = _by_kind(_parse(zpl).template, "image")[0]

    assert isinstance(element, ImageElement)
    assert element.source == "base64"
    back = images.to_graphic(element.value, 40, 24, dither=False)
    assert back.data == graphic.data
    assert back.width_dots == graphic.width_dots


def test_an_imported_graphic_carries_a_real_png():
    png = small_png()
    graphic = images.to_graphic(base64.b64encode(png).decode(), 40, 24, dither=False)
    element = _by_kind(_parse(f"^XA^PW812^LL1218^FO0,0{graphic.zpl}^FS^XZ").template, "image")[0]

    decoded = Image.open(io.BytesIO(base64.b64decode(element.value)))
    assert decoded.format == "PNG"
    assert decoded.size == (graphic.width_dots, graphic.height_dots)


def test_every_element_gets_a_name_that_says_what_it_is():
    names = [e.name for e in _parse().template.elements]

    assert len(names) == len(set(names)), "names must be unique within a template"
    assert names[0].startswith("text")
    assert any(n.startswith("barcode") for n in names)


def test_a_command_platen_does_not_know_is_a_warning_not_a_failure():
    result = _parse("^XA^PW812^LL1218^PR4,4^MNY^FO0,0^A0N,30,0^FDhi^FS^XZ")

    assert [e.value for e in result.template.elements] == ["hi"]
    assert any("^PR" in w for w in result.warnings)
    assert any("^MN" in w for w in result.warnings)


def test_zpl_with_no_label_at_all_is_refused():
    try:
        _parse("select * from cartons")
    except zplimport.NotZpl as exc:
        assert "^XA" in str(exc)
    else:
        raise AssertionError("garbage was accepted as a label")


def test_the_imported_template_renders(seeded):
    """The point of importing is to get something you can print."""
    result = _parse()
    seeded.put("/templates/imported", json=result.template.model_dump(mode="json"))

    r = seeded.post("/templates/imported/preview.zpl", json={})

    assert r.status_code == 200, r.text
    assert "7MB9042198" in r.text


# ------------------------------------------------------------ over the wire

def test_importing_saves_a_draft_and_hands_back_what_it_dropped(client):
    r = client.post("/templates/import", json={
        "id": "from_zpl", "name": "From ZPL", "dpi": 203, "zpl": LABEL + "^PR4,4"})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "from_zpl"
    assert body["elements"] == 6
    assert any("^PR" in w for w in body["warnings"])

    saved = client.get("/templates/from_zpl").json()
    assert saved["version"] == 0, "an import is a draft, not a published label"
    assert len(saved["elements"]) == 6


def test_importing_something_that_is_not_a_label_saves_nothing(client):
    r = client.post("/templates/import", json={
        "id": "junk", "name": "Junk", "zpl": "drop table cartons"})

    assert r.status_code == 422
    assert "^XA" in r.json()["detail"]
    assert client.get("/templates/junk").status_code == 404


def test_importing_does_not_quietly_overwrite_a_template(seeded):
    r = seeded.post("/templates/import", json={"id": "carton", "name": "Carton", "zpl": LABEL})

    assert r.status_code == 409
    assert "carton" in r.json()["detail"]
    assert len(seeded.get("/templates/carton").json()["elements"]) == 3


def test_a_template_survives_a_trip_out_to_zpl_and_back(client):
    """What Platen writes, Platen can read.

    Sizes come back within a dot rather than exactly: ZPL holds dots, so 100 mm
    at 203 dpi is 799 dots, which is 99.97 mm on the way home. That loss is the
    printer's, not the importer's.
    """
    original = {
        "id": "trip", "name": "Trip", "width_mm": 100, "height_mm": 150, "dpi": 203,
        "elements": [
            {"kind": "text", "name": "who", "box": {"x": 6, "y": 20, "w": 88, "h": 5},
             "value": "Coles DC Truganina", "height_pt": 14},
            {"kind": "barcode", "name": "code", "box": {"x": 6, "y": 60, "w": 88, "h": 20},
             "value": "7MB9042198", "module_dots": 3},
            {"kind": "box", "name": "frame", "box": {"x": 3, "y": 3, "w": 94, "h": 144},
             "thickness_mm": 0.5},
        ],
    }
    client.put("/templates/trip", json=original)
    zpl = client.post("/templates/trip/preview.zpl", json={}).text

    result = zplimport.parse(zpl, id="trip2", name="Trip 2", dpi=203)

    assert [e.kind for e in result.template.elements] == ["text", "barcode", "box"]
    dot = 25.4 / 203
    assert abs(result.template.width_mm - 100.0) < dot
    assert abs(result.template.height_mm - 150.0) < dot
    assert [round(e.box.x) for e in result.template.elements] == [6, 6, 3]
    assert result.template.elements[0].value == "Coles DC Truganina"
    assert result.template.elements[1].value == "7MB9042198"
    assert result.template.elements[1].module_dots == 3
