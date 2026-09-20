"""Somebody else's ZPL, read back into a template.

An importer is a guess by nature: ZPL records where the dots go, not what the
label meant. This one keeps what it understands, turns dots back into the
millimetres the editor works in, and says plainly what it dropped — a silent
importer is worse than none, because the gap only shows up on stock.
"""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

import barcode
import qrcode
from PIL import Image

from .models import (
    BarcodeElement,
    Box,
    BoxShape,
    Element,
    ImageElement,
    LineShape,
    QrElement,
    Template,
    TextElement,
)


class NotZpl(ValueError):
    """The text handed over isn't a label."""


SYMBOLOGY = {"BC": "code128", "B3": "code39", "B2": "i2of5"}
PY_SYMBOLOGY = {"code128": "code128", "gs1_128": "code128", "code39": "code39", "i2of5": "itf"}
HEX = re.compile(r"\A[0-9A-Fa-f\s]+\Z")


@dataclass
class Imported:
    template: Template
    warnings: list[str] = field(default_factory=list)


def parse(zpl: str, *, id: str, name: str, dpi: int = 203) -> Imported:
    return _Reader(zpl, id=id, name=name, dpi=dpi).run()


# --------------------------------------------------------------- tokenising

def _tokens(zpl: str) -> Iterator[tuple[str, str]]:
    """(code, parameters), in order. ^FD swallows everything up to ^FS, since
    its data is arbitrary text and not a parameter list."""
    i, n = 0, len(zpl)
    while i < n:
        marks = [p for p in (zpl.find("^", i), zpl.find("~", i)) if p != -1]
        if not marks:
            return
        c = min(marks)
        sigil, code = zpl[c], zpl[c + 1 : c + 3].upper()
        if sigil == "^" and code == "FD":
            end = zpl.find("^FS", c)
            end = n if end == -1 else end
            yield "FD", zpl[c + 3 : end]
            i = end
            continue
        k = c + 3
        while k < n and zpl[k] not in "^~":
            k += 1
        yield ("~" + code if sigil == "~" else code), zpl[c + 3 : k].strip()
        i = k


def _ints(params: str, count: int, default: int = 0) -> list[int]:
    out = []
    for i in range(count):
        part = params.split(",")[i].strip() if i < len(params.split(",")) else ""
        try:
            out.append(int(float(part)))
        except ValueError:
            out.append(default)
    return out


def _part(params: str, i: int, default: str = "") -> str:
    bits = params.split(",")
    return bits[i].strip().upper() if i < len(bits) and bits[i].strip() else default


# ------------------------------------------------------------- the one field

@dataclass
class _Field:
    x: int = 0
    y: int = 0
    baseline: bool = False         # ^FT gives the baseline, ^FO the top-left
    font: str | None = None
    char_h: int = 0
    char_w: int = 0
    rotation: int = 0
    block: tuple[int, int, str] | None = None
    unescape: str | None = None    # ^FH's escape character
    barcode: dict[str, Any] | None = None
    qr: dict[str, Any] | None = None
    data: str | None = None


class _Reader:
    def __init__(self, zpl: str, *, id: str, name: str, dpi: int) -> None:
        if "^XA" not in zpl.upper():
            raise NotZpl("this doesn't look like a label: no ^XA in it")
        self.zpl, self.id, self.name, self.dpi = zpl, id, name, dpi
        self.width_dots, self.height_dots, self.darkness = 0, 0, None
        self.home = (0, 0)
        self.by = (2, 3.0, 10)          # ^BY module width, ratio, height
        self.default_font: tuple[str, int, int] | None = None
        self.elements: list[Element] = []
        self.warnings: list[str] = []
        self.counts: dict[str, int] = {}
        self.f: _Field | None = None

    # --- units
    def mm(self, dots: float) -> float:
        return round(dots * 25.4 / self.dpi, 2)

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def name_for(self, kind: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return f"{kind}_{self.counts[kind]}"

    # --- the walk
    def run(self) -> Imported:
        for code, params in _tokens(self.zpl):
            self.command(code, params)
        self.flush()

        width = self.mm(self.width_dots) if self.width_dots else 101.6
        height = self.mm(self.height_dots) if self.height_dots else 152.4
        if not self.width_dots:
            self.warn("no ^PW in the label, so its width is a guess at 101.6 mm")
        if not self.height_dots:
            self.warn("no ^LL in the label, so its height is a guess at 152.4 mm")

        template = Template(
            id=self.id, name=self.name, width_mm=width, height_mm=height,
            dpi=self.dpi, darkness=self.darkness, elements=self.elements,
        )
        return Imported(template=template, warnings=self.warnings)

    def command(self, code: str, params: str) -> None:
        if code in ("XA", "XZ", "FS"):
            self.flush()
            return
        if code == "PW":
            self.width_dots = _ints(params, 1)[0]
        elif code == "LL":
            self.height_dots = _ints(params, 1)[0]
        elif code == "MD":
            self.darkness = _ints(params, 1)[0]
        elif code == "LH":
            self.home = tuple(_ints(params, 2))          # type: ignore[assignment]
        elif code == "BY":
            w, ratio, h = params.split(",") + [""] * (3 - len(params.split(",")))
            self.by = (int(w or 2), float(ratio or 3), int(h or self.by[2]))
        elif code == "CF":
            f = _part(params, 0, "0")
            h, w = _ints(params, 3)[1:]
            self.default_font = (f, h, w)
        elif code in ("FO", "FT"):
            self.flush()
            x, y = _ints(params, 2)
            self.f = _Field(x=x + self.home[0], y=y + self.home[1], baseline=code == "FT")
            if self.default_font:
                self.f.font, self.f.char_h, self.f.char_w = self.default_font
        elif code.startswith("A") and len(code) == 2:
            self.field().font = code[1]
            self.field().rotation = _rotation(_part(params, 0, "N"))
            h, w = _ints(params, 3)[1:]
            self.field().char_h, self.field().char_w = h, w
        elif code == "FB":
            w, lines = _ints(params, 2)
            self.field().block = (w, max(lines, 1), _part(params, 3, "L"))
        elif code == "FH":
            self.field().unescape = (params or "_")[0]
        elif code in SYMBOLOGY:
            self.field().barcode = {"kind": SYMBOLOGY[code], "params": params}
            self.field().rotation = _rotation(_part(params, 0, "N"))
        elif code == "BQ":
            self.field().qr = {"params": params}
        elif code == "GB":
            self.graphic_box(params)
        elif code == "GF":
            self.graphic(params)
        elif code == "FD":
            self.field().data = params
        elif code in ("CI", "FW", "PO", "LS", "LT", "LR", "PM", "FR", "JM", "MM", "MN",
                      "MT", "PR", "SN", "PQ", "XG", "GS", "BX", "B7", "B8", "B9", "BA",
                      "BE", "BK", "BL", "BM", "BO", "BP", "BR", "BS", "BT", "BU", "BZ"):
            self.warn(f"^{code} isn't imported, so the label may not match exactly")
        elif code.startswith("~"):
            self.warn(f"~{code[1:]} is a printer command, not part of the label; skipped")
        else:
            self.warn(f"^{code} isn't understood and was skipped")

    def field(self) -> _Field:
        if self.f is None:
            self.f = _Field()
        return self.f

    # --- turning a finished field into an element
    def flush(self) -> None:
        f, self.f = self.f, None
        if f is None or f.data is None:
            return
        data = _unescape(f.data, f.unescape)
        if f.barcode:
            self.elements.append(self.barcode(f, data))
        elif f.qr:
            self.elements.append(self.qr(f, data))
        elif f.font or f.char_h:
            self.elements.append(self.text(f, data))
        else:
            self.warn(f"a ^FD with no font or barcode before it was skipped: {data[:24]!r}")

    def box(self, f: _Field, w_dots: float, h_dots: float, lift: float = 0) -> Box:
        y = f.y - lift if f.baseline else f.y
        return Box(x=self.mm(f.x), y=self.mm(max(y, 0)),
                   w=self.mm(w_dots), h=self.mm(h_dots), rotation=f.rotation)

    def text(self, f: _Field, data: str) -> TextElement:
        h = f.char_h or 30
        width = f.block[0] if f.block else max(self.width_dots - f.x, h * len(data) * 0.6)
        align = {"L": "left", "C": "centre", "R": "right"}.get(f.block[2] if f.block else "L", "left")
        return TextElement(
            name=self.name_for("text"), box=self.box(f, width, h, lift=h), value=data,
            font=f.font or "0", height_pt=round(h * 72 / self.dpi, 1),
            width_pt=round(f.char_w * 72 / self.dpi, 1) if f.char_w else None,
            align=align, multiline=bool(f.block and f.block[1] > 1),
        )

    def barcode(self, f: _Field, data: str) -> BarcodeElement:
        p = f.barcode["params"]                      # type: ignore[index]
        kind = f.barcode["kind"]                     # type: ignore[index]
        if data.startswith(">;"):
            kind, data = "gs1_128", data[2:]
        height = _ints(p, 2)[1] or self.by[2]
        line, above = _part(p, 2, "Y"), _part(p, 3, "N")
        readable = "none" if line != "Y" else ("above" if above == "Y" else "below")
        module = self.by[0]
        return BarcodeElement(
            name=self.name_for("barcode"),
            box=self.box(f, self.barcode_width(kind, data, module), height, lift=height),
            value=data, symbology=kind, module_dots=module, human_readable=readable,
            check_digit=_part(p, 4, "N") == "Y",
        )

    def barcode_width(self, kind: str, data: str, module: int) -> float:
        """How wide it will actually print. ZPL never says, because the bars
        follow from the data — so ask the same encoder the preview uses."""
        try:
            modules = "".join(barcode.get_barcode_class(PY_SYMBOLOGY[kind])(data).build())
            return len(modules) * module
        except Exception:                            # noqa: BLE001 — a hint, not a contract
            return len(data) * 11 * module

    def qr(self, f: _Field, data: str) -> QrElement:
        p = f.qr["params"]                           # type: ignore[index]
        model = _ints(p, 2)[1] or 2
        magnification = _ints(p, 3)[2] or 5
        correction, _, rest = data.partition(",")
        level = correction[:1] if correction[:1] in ("L", "M", "Q", "H") else "Q"
        value = rest if correction else data
        return QrElement(
            name=self.name_for("qr"), box=self.box(f, *(self.qr_size(value, magnification),) * 2),
            value=value, model=model if model in (1, 2) else 2,
            magnification=magnification, error_correction=level,
        )

    def qr_size(self, data: str, magnification: int) -> float:
        try:
            q = qrcode.QRCode(border=0)
            q.add_data(data)
            q.make(fit=True)
            return len(q.get_matrix()) * magnification
        except Exception:                            # noqa: BLE001
            return 25 * magnification

    def graphic_box(self, params: str) -> None:
        f = self.field()
        w, h, t = _ints(params, 3)
        t = max(t, 1)
        w, h = max(w, t), max(h, t)
        common = {"box": self.box(f, w, h), "name": ""}
        if min(w, h) <= t:
            common["name"] = self.name_for("line")
            self.elements.append(LineShape(thickness_mm=self.mm(t), **common))
        else:
            common["name"] = self.name_for("box")
            self.elements.append(BoxShape(thickness_mm=self.mm(t), fill=False, **common))
        self.f = None

    def graphic(self, params: str) -> None:
        f = self.field()
        bits = params.split(",", 4)
        if len(bits) < 5 or bits[0].strip().upper() != "A":
            self.warn("only ^GFA graphics are imported; this one was skipped")
            self.f = None
            return
        total, _, row_bytes, data = int(bits[1]), bits[2], int(bits[3]), bits[4]
        if not HEX.fullmatch(data):
            self.warn("a ^GFA using ZPL's compressed encoding was skipped")
            self.f = None
            return
        raw = bytes.fromhex("".join(data.split()))
        height = (total or len(raw)) // row_bytes
        image = Image.new("1", (row_bytes * 8, height), 1)
        pixels = image.load()
        for y in range(height):
            for x in range(row_bytes * 8):
                byte = raw[y * row_bytes + (x >> 3)]
                pixels[x, y] = 0 if byte & (0x80 >> (x & 7)) else 1
        out = io.BytesIO()
        image.save(out, "PNG")
        self.elements.append(ImageElement(
            name=self.name_for("image"),
            box=self.box(f, row_bytes * 8, height),
            value=base64.b64encode(out.getvalue()).decode(),
            source="base64", dither=False,
        ))
        self.f = None


def _rotation(letter: str) -> int:
    return {"N": 0, "R": 90, "I": 180, "B": 270}.get(letter, 0)


def _unescape(data: str, char: str | None) -> str:
    """^FH says the data carries _XX hex escapes; put the characters back."""
    if not char:
        return data
    out, i = bytearray(), 0
    while i < len(data):
        if data[i] == char and i + 2 < len(data) + 1:
            try:
                out.append(int(data[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.extend(data[i].encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")
