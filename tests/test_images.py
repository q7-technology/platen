"""base64 in, printer dots out — including the shapes real data arrives in."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app import images


def _png(mode: str = "L", colour=255, size=(32, 24)) -> bytes:
    img = Image.new(mode, size, colour)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_a_bytea_column_arrives_as_bytes_not_base64():
    """Postgres bytea and a few ORMs hand over the image itself."""
    graphic = images.to_graphic(_png(), 32, 24)

    assert graphic.width_dots == 32


def test_a_memoryview_is_the_same_thing():
    assert images.decode(memoryview(_png())).startswith(b"\x89PNG")


def test_a_browsers_data_uri_is_unwrapped():
    raw = _png()
    uri = "data:image/png;base64," + base64.b64encode(raw).decode()

    assert images.decode(uri) == raw


def test_base64_wrapped_across_lines_is_fine():
    """encode(col,'base64') in Postgres wraps at 76 characters."""
    encoded = base64.encodebytes(_png()).decode()

    assert "\n" in encoded
    assert images.decode(encoded).startswith(b"\x89PNG")


def test_something_that_is_not_base64_says_so():
    with pytest.raises(images.NotAnImage) as exc:
        images.decode("this is just a sentence")

    assert "base64" in str(exc.value)


def test_base64_of_something_that_is_not_an_image_says_so():
    with pytest.raises(images.NotAnImage) as exc:
        images.decode(base64.b64encode(b"hello there, not a picture").decode())

    assert "image" in str(exc.value)


def test_a_transparent_background_comes_out_white_not_black():
    """Transparent pixels dither to black if nobody flattens them first, and
    a compliance mark printed as a solid block is worse than none."""
    img = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")

    graphic = images.to_graphic(base64.b64encode(buf.getvalue()).decode(), 24, 24,
                                dither=False)

    assert graphic.data == bytes(len(graphic.data)), "transparency burned onto the label"


def test_a_palette_image_is_flattened_too():
    img = Image.new("P", (16, 16))
    buf = io.BytesIO()
    img.save(buf, "PNG")

    graphic = images.to_graphic(base64.b64encode(buf.getvalue()).decode(), 16, 16)

    assert graphic.width_dots == 16


def test_dithering_is_the_default_and_does_something():
    """A mid-grey has no threshold answer, so dithering is what makes it
    printable at all."""
    grey = base64.b64encode(_png(colour=128, size=(16, 16))).decode()

    dithered = images.to_graphic(grey, 16, 16, dither=True)
    thresholded = images.to_graphic(grey, 16, 16, dither=False, threshold=128)

    assert dithered.data != thresholded.data
    assert set(dithered.data) != {0}, "the whole thing came out white"
    assert set(dithered.data) != {255}, "the whole thing came out black"


def test_a_hard_threshold_does_what_it_says():
    light = base64.b64encode(_png(colour=200, size=(16, 16))).decode()

    assert images.to_graphic(light, 16, 16, dither=False, threshold=128).data == \
        bytes(2 * 16)
    assert set(images.to_graphic(light, 16, 16, dither=False, threshold=220).data) == {0xFF}


def test_an_image_is_never_blown_up_past_its_own_size():
    """Scaling a small mark up to fill a box would print it blurry."""
    graphic = images.to_graphic(_png(size=(20, 10)), 400, 200)

    assert (graphic.width_dots, graphic.height_dots) == (20, 10)


def test_what_it_sniffs():
    assert images.sniff(b"\x89PNG\r\n\x1a\n") == "png"
    assert images.sniff(b"\xff\xd8\xffhello") == "jpeg"
    assert images.sniff(b"GIF89a") == "gif"
    assert images.sniff(b"nope") is None
