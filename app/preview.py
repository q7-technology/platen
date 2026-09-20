"""The same element tree, drawn with Pillow instead of sent to a printer.

Rendered at the printer's real dot pitch so what the browser shows is what the
label will be — including the dithering, which is the part people are surprised
by when they see it on paper.
"""

from __future__ import annotations

import io
from typing import Any, Mapping

import barcode
import qrcode
from PIL import Image, ImageDraw, ImageFont

from . import images
from .binding import BindingError, MissingField, raw_value, render_value
from .models import (
    BarcodeElement,
    Element,
    BoxShape,
    ImageElement,
    LineShape,
    QrElement,
    Template,
    TextElement,
)

SYMBOLOGY = {"code128": "code128", "gs1_128": "code128", "code39": "code39", "i2of5": "itf"}


class RenderError(Exception):
    """Same contract as the ZPL renderer's: name the element and the reason."""


def _font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _barcode(draw: ImageDraw.ImageDraw, el: BarcodeElement, value: str,
             x: int, y: int, h: int) -> None:
    """Draw the bars the way the print head will lay them down.

    ^BC takes a narrow-bar width and a bar height and draws whatever width
    that comes to — it never scales a barcode to fit a box. So neither do we:
    one module is `module_dots` wide, the bars get the box height to
    themselves, and the readable line goes underneath, outside that height.
    A barcode too wide for its box overflows here exactly as it would on the
    label, which is the point of looking at a preview.
    """
    code = barcode.get_barcode_class(SYMBOLOGY[el.symbology])(value)
    modules = "".join(code.build())
    mw = max(1, el.module_dots)

    for i, module in enumerate(modules):
        if module == "1":
            draw.rectangle([x + i * mw, y, x + (i + 1) * mw - 1, y + h - 1], fill=0)

    if el.human_readable == "none":
        return
    px = max(12, round(h * 0.22))
    font = _font(px)
    text = code.get_fullcode()
    width = draw.textlength(text, font=font)
    tx = x + (len(modules) * mw - width) / 2
    ty = y + h + max(2, px // 5) if el.human_readable == "below" else y - px - max(2, px // 5)
    draw.text((tx, ty), text, fill=0, font=font)


def render_png(template: Template, row: Mapping[str, Any]) -> bytes:
    d = template.dots
    canvas = Image.new("L", (template.width_dots, template.height_dots), 255)
    draw = ImageDraw.Draw(canvas)

    for el in template.elements:
        if el.hidden:
            continue
        try:
            _element(canvas, draw, template, el, row)
        except MissingField as exc:
            raise RenderError(f"{el.name}: the query returned no column {exc.args[0]!r}") from exc
        except BindingError as exc:
            raise RenderError(f"{el.name}: {exc}") from exc

    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


def _element(canvas: Image.Image, draw: ImageDraw.ImageDraw, template: Template,
             el: Element, row: Mapping[str, Any]) -> None:
    """One element onto the canvas. Raises like the ZPL renderer does."""
    d = template.dots
    x, y = d(el.box.x), d(el.box.y)
    w, h = d(el.box.w), d(el.box.h)

    if isinstance(el, TextElement):
        draw.text((x, y), render_value(el.value, row), fill=0,
                  font=_font(d(el.height_pt * 25.4 / 72)))

    elif isinstance(el, BarcodeElement):
        value = render_value(el.value, row).strip()
        if value:
            _barcode(draw, el, value, x, y, h)

    elif isinstance(el, QrElement):
        value = render_value(el.value, row)
        img = qrcode.make(value, border=1).convert("L").resize((w, w))
        canvas.paste(img, (x, y))

    elif isinstance(el, ImageElement):
        value = raw_value(el.value, row)
        if value:
            try:
                g = images.to_graphic(value, w, h, dither=el.dither,
                                      threshold=el.threshold)
                bitmap = Image.frombytes(
                    "1", (g.row_bytes * 8, g.height_dots), g.data
                ).crop((0, 0, g.width_dots, g.height_dots))
                # ^GFA is 1=black; PIL '1' is 0=black
                canvas.paste(bitmap.point(lambda p: 0 if p else 255, "L"), (x, y))
            except images.NotAnImage:
                draw.rectangle([x, y, x + w, y + h], outline=128, width=2)

    elif isinstance(el, BoxShape):
        draw.rectangle([x, y, x + w, y + h], outline=0,
                       width=d(el.thickness_mm), fill=0 if el.fill else None)

    elif isinstance(el, LineShape):
        draw.rectangle([x, y, x + max(w, 1), y + max(h, d(el.thickness_mm))], fill=0)
