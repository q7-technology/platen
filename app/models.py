"""Template schema. A template is data, not code — it round-trips to JSON and
that JSON is what the editor in the browser reads and writes."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

Rotation = Literal[0, 90, 180, 270]


def mm_to_dots(mm: float, dpi: int) -> int:
    return round(mm * dpi / 25.4)


class Box(BaseModel):
    """Where an element sits, in millimetres from the top-left of the label."""

    x: float
    y: float
    w: float
    h: float
    rotation: Rotation = 0


class _Element(BaseModel):
    name: str                      # unique within the template; shown in Layers
    box: Box
    hidden: bool = False


class TextElement(_Element):
    kind: Literal["text"] = "text"
    value: str                     # literal, or a binding like "{{ order.sku }}"
    font: str = "0"                # ZPL font slot; "0" is the scalable default
    height_pt: float = 10.0
    width_pt: float | None = None  # None = proportional
    align: Literal["left", "centre", "right"] = "left"
    multiline: bool = False


class BarcodeElement(_Element):
    kind: Literal["barcode"] = "barcode"
    value: str
    symbology: Literal["code128", "code39", "gs1_128", "i2of5"] = "code128"
    module_dots: int = 2           # narrow bar width
    human_readable: Literal["none", "above", "below"] = "below"
    check_digit: bool = False
    skip_if_blank: bool = True     # a blank barcode prints as garbage — skip the label


class QrElement(_Element):
    kind: Literal["qr"] = "qr"
    value: str
    model: Literal[1, 2] = 2
    magnification: int = 5
    error_correction: Literal["L", "M", "Q", "H"] = "Q"


class DataMatrixElement(_Element):
    """^BX. The print head does the ECC200 encoding; we hand it the data."""

    kind: Literal["datamatrix"] = "datamatrix"
    value: str
    module_dots: int = 6           # the side of one square module
    quality: Literal[0, 50, 80, 100, 140, 200] = 200   # 200 is ECC200
    skip_if_blank: bool = True


class ImageElement(_Element):
    """An image that comes from the data, not from the template.

    `source="base64"` is the common case: the query returns a base64 string in
    a column and we turn it into printer dots at render time.
    """

    kind: Literal["image"] = "image"
    value: str
    source: Literal["base64", "url", "asset"] = "base64"
    dither: bool = True            # Floyd–Steinberg; off = hard threshold
    threshold: int = 128           # only used when dither is False
    on_missing: Literal["blank", "placeholder", "fail"] = "blank"


class BoxShape(_Element):
    kind: Literal["box"] = "box"
    thickness_mm: float = 0.4
    fill: bool = False


class LineShape(_Element):
    kind: Literal["line"] = "line"
    thickness_mm: float = 0.4


Element = Annotated[
    TextElement | BarcodeElement | QrElement | DataMatrixElement
    | ImageElement | BoxShape | LineShape,
    Field(discriminator="kind"),
]


class Template(BaseModel):
    id: str
    name: str
    version: int = 1
    width_mm: float
    height_mm: float
    dpi: Literal[203, 300, 600] = 203
    darkness: int | None = None    # None = leave the printer's own setting alone
    folder: str = ""               # how the library groups it; "" is unfiled
    datasource: str | None = None  # name in the datasource registry
    query: str | None = None       # name of a saved query on that datasource
    elements: list[Element] = Field(default_factory=list)

    def dots(self, mm: float) -> int:
        return mm_to_dots(mm, self.dpi)

    @property
    def width_dots(self) -> int:
        return self.dots(self.width_mm)

    @property
    def height_dots(self) -> int:
        return self.dots(self.height_mm)
