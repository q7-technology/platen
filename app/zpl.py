"""Element tree + one row -> one label's worth of ZPL."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from . import images
from .binding import BindingError, MissingField, raw_value, render_value
from .models import (
    BarcodeElement,
    BoxShape,
    DataMatrixElement,
    ImageElement,
    LineShape,
    QrElement,
    Template,
    TextElement,
)

BARCODE_CMD = {
    "code128": "^BCN,{h},{hr},N,N",
    "code39": "^B3N,N,{h},{hr},N",
    "gs1_128": "^BCN,{h},{hr},N,N,A",
    "i2of5": "^B2N,{h},{hr},N,N",
}


class RenderError(Exception):
    """Raised before anything is sent to a printer."""


@dataclass
class Label:
    zpl: str
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False


def _esc(text: str) -> str:
    """Caret and tilde are control characters in ZPL; ^FH lets us hex them."""
    out = []
    for ch in text:
        if ch in "^~\\" or ord(ch) > 126:
            out.extend(f"_{b:02X}" for b in ch.encode("utf-8"))
        else:
            out.append(ch)
    return "".join(out)


class ZplRenderer:
    def __init__(self, template: Template) -> None:
        self.t = template

    def render(self, row: Mapping[str, Any]) -> Label:
        warnings: list[str] = []
        body: list[str] = []

        for el in self.t.elements:
            if el.hidden:
                continue
            try:
                chunk = self._element(el, row, warnings)
            except MissingField as exc:
                raise RenderError(
                    f"{el.name}: the query returned no column {exc.args[0]!r}"
                ) from exc
            except BindingError as exc:
                raise RenderError(f"{el.name}: {exc}") from exc
            if chunk is None:
                return Label(zpl="", warnings=warnings, skipped=True)
            body.append(chunk)

        head = [f"^XA^PW{self.t.width_dots}^LL{self.t.height_dots}^LH0,0^LT0"]
        if self.t.darkness is not None:
            head.append(f"^MD{self.t.darkness}")
        return Label(zpl="\n".join(head + body + ["^XZ"]), warnings=warnings)

    # one method per element kind; each returns ZPL, or None to skip the label

    def _element(self, el, row, warnings) -> str | None:
        d, box = self.t.dots, el.box
        x, y = d(box.x), d(box.y)
        w, h = d(box.w), d(box.h)
        o = {0: "N", 90: "R", 180: "I", 270: "B"}[box.rotation]

        if isinstance(el, TextElement):
            text = _esc(render_value(el.value, row))
            ch = d(el.height_pt * 25.4 / 72)
            cw = d(el.width_pt * 25.4 / 72) if el.width_pt else 0
            block = f"^FB{w},{4 if el.multiline else 1},0,{el.align[0].upper()}"
            return f"^FO{x},{y}^A{el.font}{o},{ch},{cw}{block}^FH_^FD{text}^FS"

        if isinstance(el, BarcodeElement):
            data = render_value(el.value, row).strip()
            if not data:
                if el.skip_if_blank:
                    warnings.append(f"{el.name} is blank — label skipped")
                    return None
                warnings.append(f"{el.name} is blank")
                return ""
            hr = {"none": "N", "above": "Y", "below": "Y"}[el.human_readable]
            above = "Y" if el.human_readable == "above" else "N"
            cmd = BARCODE_CMD[el.symbology].format(h=h, hr=hr)
            prefix = ">;" if el.symbology == "gs1_128" else ""
            return (
                f"^FO{x},{y}^BY{el.module_dots},3,{h}"
                f"{cmd.replace('N,N', f'{above},N', 1) if above == 'Y' else cmd}"
                f"^FD{prefix}{_esc(data)}^FS"
            )

        if isinstance(el, DataMatrixElement):
            data = render_value(el.value, row).strip()
            if not data:
                if el.skip_if_blank:
                    warnings.append(f"{el.name} is blank — label skipped")
                    return None
                warnings.append(f"{el.name} is blank")
                return ""
            return f"^FO{x},{y}^BX{o},{el.module_dots},{el.quality}^FD{_esc(data)}^FS"

        if isinstance(el, QrElement):
            data = _esc(render_value(el.value, row))
            return (
                f"^FO{x},{y}^BQ{o},{el.model},{el.magnification}"
                f"^FD{el.error_correction}A,{data}^FS"
            )

        if isinstance(el, ImageElement):
            return self._image(el, row, warnings, x, y, w, h)

        if isinstance(el, BoxShape):
            t = d(el.thickness_mm)
            return f"^FO{x},{y}^GB{w},{h},{h if el.fill else t}^FS"

        if isinstance(el, LineShape):
            t = d(el.thickness_mm)
            return f"^FO{x},{y}^GB{max(w, t)},{max(h, t)},{t}^FS"

        raise RenderError(f"{el.name}: unsupported element")

    def _image(self, el: ImageElement, row, warnings, x, y, w, h) -> str | None:
        value = raw_value(el.value, row)
        if value in (None, ""):
            if el.on_missing == "fail":
                raise RenderError(f"{el.name}: no image in this row")
            if el.on_missing == "placeholder":
                return f"^FO{x},{y}^GB{w},{h},2^FS"
            warnings.append(f"{el.name}: no image in this row")
            return ""
        try:
            g = images.to_graphic(
                value, w, h, dither=el.dither, threshold=el.threshold
            )
        except images.NotAnImage as exc:
            if el.on_missing == "fail":
                raise RenderError(f"{el.name}: {exc}") from exc
            warnings.append(f"{el.name}: {exc}")
            return ""
        return f"^FO{x},{y}{g.zpl}^FS"


def separator(template: Template, run_id: str, labels: int, when: str) -> str:
    """A divider so a despatch bench can tell where one job stopped and the
    next began. Built, not bound, so it cannot fail a render."""
    d = template.dots
    margin = d(6)
    size = d(9)
    return "\n".join([
        f"^XA^PW{template.width_dots}^LL{template.height_dots}^LH0,0^LT0",
        f"^FO{margin},{d(12)}^GB{template.width_dots - margin * 2},{d(0.8)},{d(0.8)}^FS",
        f"^FO{margin},{d(18)}^A0N,{size * 2},0^FD{_esc(run_id)}^FS",
        f"^FO{margin},{d(34)}^A0N,{size},0^FD{labels} labels^FS",
        f"^FO{margin},{d(44)}^A0N,{size},0^FD{_esc(when)}^FS",
        f"^FO{margin},{d(56)}^A0N,{size},0^FDEND OF JOB^FS",
        f"^FO{margin},{d(66)}^GB{template.width_dots - margin * 2},{d(0.8)},{d(0.8)}^FS",
        "^XZ",
    ])


def render_run(template: Template, rows: list[Mapping[str, Any]], copies: int = 1):
    """Render every label before a single byte goes near a printer."""
    labels, warnings = [], []
    for i, row in enumerate(rows, start=1):
        try:
            label = ZplRenderer(template).render(row)
        except RenderError as exc:
            raise RenderError(f"row {i}: {exc}") from exc
        warnings += [f"row {i}: {w}" for w in label.warnings]
        if not label.skipped:
            labels.extend([label.zpl] * copies)
    return labels, warnings
