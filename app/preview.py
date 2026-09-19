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
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

from . import images
from .binding import raw_value, render_value
from .models import (
    BarcodeElement,
    BoxShape,
    ImageElement,
    LineShape,
    QrElement,
    Template,
    TextElement,
)

SYMBOLOGY = {"code128": "code128", "gs1_128": "code128", "code39": "code39", "i2of5": "itf"}


def _font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/Library/Fonts/Arial.ttf"):
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    return ImageFont.load_default()


def render_png(template: Template, row: Mapping[str, Any]) -> bytes:
    d = template.dots
    canvas = Image.new("L", (template.width_dots, template.height_dots), 255)
    draw = ImageDraw.Draw(canvas)

    for el in template.elements:
        if el.hidden:
            continue
        x, y = d(el.box.x), d(el.box.y)
        w, h = d(el.box.w), d(el.box.h)

        if isinstance(el, TextElement):
            draw.text((x, y), render_value(el.value, row), fill=0,
                      font=_font(d(el.height_pt * 25.4 / 72)))

        elif isinstance(el, BarcodeElement):
            value = render_value(el.value, row).strip()
            if value:
                cls = barcode.get_barcode_class(SYMBOLOGY[el.symbology])
                buf = io.BytesIO()
                cls(value, writer=ImageWriter()).write(
                    buf, {"module_height": el.box.h, "write_text": el.human_readable != "none"}
                )
                canvas.paste(Image.open(buf).convert("L").resize((w, h)), (x, y))

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

    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()
