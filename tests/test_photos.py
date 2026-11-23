import io

import pillow_heif
import pytest
from PIL import Image

from olx_auto import photos
from tests.conftest import jpeg_bytes


def _exif_with_gps_and_rotation() -> bytes:
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° CW to display
    exif[0x8825] = {1: "N", 2: (42.0, 41.0, 0.0), 3: "E", 4: (23.0, 19.0, 0.0)}  # GPS IFD
    return exif.tobytes()


def test_strips_exif_and_applies_orientation():
    raw = jpeg_bytes(size=(80, 40), exif=_exif_with_gps_and_rotation())
    assert Image.open(io.BytesIO(raw)).getexif()  # precondition: input has EXIF

    out = Image.open(io.BytesIO(photos.process(raw)))
    assert out.format == "JPEG"
    assert not out.getexif()
    assert out.size == (40, 80)  # rotated upright


def test_resizes_long_edge():
    out = Image.open(io.BytesIO(photos.process(jpeg_bytes(size=(4000, 1000)))))
    assert out.size == (2000, 500)


def test_converts_png_with_alpha():
    buf = io.BytesIO()
    Image.new("RGBA", (10, 10), (0, 0, 255, 128)).save(buf, "PNG")
    assert Image.open(io.BytesIO(photos.process(buf.getvalue()))).mode == "RGB"


def test_converts_heic():
    buf = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (32, 32), "green")).save(buf, format="HEIF")
    assert Image.open(io.BytesIO(photos.process(buf.getvalue()))).format == "JPEG"


def test_rejects_non_image():
    with pytest.raises(photos.InvalidPhoto):
        photos.process(b"not an image")
