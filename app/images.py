"""base64 -> printer dots.

A Zebra printer has no idea what a PNG is. ^GFA takes a raw 1-bit bitmap: one
bit per dot, 1 = burn, packed 8 dots to a byte, padded out to a whole byte at
the end of every row. This module is the whole reason the backend needs Pillow.
"""

from __future__ import annotations

import base64
import binascii
import io
from dataclasses import dataclass

from PIL import Image

MAGIC = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"BM": "bmp",
}


class NotAnImage(ValueError):
    pass


@dataclass(frozen=True)
class Graphic:
    """Everything ^GFA needs."""

    width_dots: int
    height_dots: int
    row_bytes: int
    data: bytes

    @property
    def zpl(self) -> str:
        total = len(self.data)
        return f"^GFA,{total},{total},{self.row_bytes},{self.data.hex().upper()}"


def sniff(raw: bytes) -> str | None:
    for magic, kind in MAGIC.items():
        if raw.startswith(magic):
            return kind
    return None


def decode(value: str | bytes | memoryview) -> bytes:
    """Accept what databases and APIs actually hand you.

    Postgres `encode(col,'base64')` wraps at 76 chars, some ORMs give you the
    bytes directly, and browsers send `data:image/png;base64,...`.
    """
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, bytes) and sniff(value):
        return value                                  # already binary
    text = value.decode() if isinstance(value, bytes) else value
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    text = "".join(text.split())                      # strip newlines/spaces
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise NotAnImage("value is not valid base64") from exc
    if sniff(raw) is None:
        raise NotAnImage("decoded bytes are not a known image format")
    return raw


def to_graphic(
    value: str | bytes,
    width_dots: int,
    height_dots: int,
    *,
    dither: bool = True,
    threshold: int = 128,
) -> Graphic:
    """base64 (or bytes) -> a 1-bit bitmap scaled to fit the element's box."""
    img = Image.open(io.BytesIO(decode(value)))

    if img.mode in ("RGBA", "LA", "P"):
        # transparent pixels would otherwise dither to black
        img = img.convert("RGBA")
        flat = Image.new("RGBA", img.size, (255, 255, 255, 255))
        flat.alpha_composite(img)
        img = flat

    img = img.convert("L")
    img.thumbnail((width_dots, height_dots), Image.LANCZOS)

    if dither:
        img = img.convert("1")                        # Floyd–Steinberg by default
    else:
        img = img.point(lambda p: 255 if p >= threshold else 0, mode="1")

    return _pack(img)


def _pack(img: Image.Image) -> Graphic:
    """PIL '1' mode is 0=black; ZPL wants 1=black. Pack rows, MSB first."""
    w, h = img.size
    row_bytes = (w + 7) // 8
    px = img.load()
    out = bytearray(row_bytes * h)

    for y in range(h):
        base = y * row_bytes
        for x in range(w):
            if px[x, y] == 0:                         # black pixel -> burn
                out[base + (x >> 3)] |= 0x80 >> (x & 7)

    return Graphic(width_dots=w, height_dots=h, row_bytes=row_bytes, data=bytes(out))
