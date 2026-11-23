"""Photo processing on upload: decode (incl. HEIC), auto-rotate, resize, re-encode as JPEG.

Re-encoding without passing `exif=` drops all metadata, including GPS coordinates.
"""
import io

import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

pillow_heif.register_heif_opener()

MAX_EDGE = 2000
JPEG_QUALITY = 85


class InvalidPhoto(ValueError):
    pass


def process(data: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise InvalidPhoto("Not a supported image (JPEG, PNG, HEIC, WebP)") from e
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()
