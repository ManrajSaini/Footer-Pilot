"""Image handling. Every image is decoded and re-encoded before it is stored.

Re-encoding drops metadata (such as GPS position in EXIF) and guarantees that what we
host really is the image type we say it is.
"""

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

PHOTO_SIZE = 160  # px, square. Shown at 72 px in signatures, so this covers 2x screens.
MAX_PIXELS = 40_000_000  # refuse absurdly large images before decoding them fully

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
UPLOAD_FORMATS = frozenset({"PNG", "JPEG", "GIF"})
# Logos are shown at up to 200 px wide; twice that keeps them sharp on dense screens.
LOGO_MAX_SIZE = (400, 400)


class InvalidImageError(ValueError):
    pass


@dataclass(frozen=True)
class ProcessedImage:
    data: bytes
    content_type: str
    extension: str
    width: int
    height: int

    @property
    def digest(self) -> str:
        """Short content hash, for storage keys."""
        return hashlib.sha256(self.data).hexdigest()[:20]


def _open(data: bytes) -> tuple[Image.Image, str | None]:
    """Decode an image, upright. Returns it with the format it arrived in."""
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            raise InvalidImageError("image is too large")
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise InvalidImageError("not a readable image") from error
    # exif_transpose returns a copy, which no longer knows its source format.
    return ImageOps.exif_transpose(image), image.format


def _flatten(image: Image.Image) -> Image.Image:
    """RGB with any transparency placed on white (JPEG has no alpha channel)."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def normalise_logo(data: bytes) -> ProcessedImage:
    """An uploaded logo, shrunk to fit LOGO_MAX_SIZE and re-encoded.

    Accepts PNG, JPEG and GIF only (SVG and anything else is refused). JPEGs stay
    JPEG; PNG and GIF become PNG so transparency survives. An animated GIF keeps its
    first frame.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise InvalidImageError("image is larger than 2 MB")
    image, source_format = _open(data)
    if source_format not in UPLOAD_FORMATS:
        raise InvalidImageError("use a PNG, JPEG or GIF image")

    output = io.BytesIO()
    if source_format == "JPEG":
        image = image.convert("RGB")
        image.thumbnail(LOGO_MAX_SIZE, Image.Resampling.LANCZOS)
        image.save(output, format="JPEG", quality=90, optimize=True)
        content_type, extension = "image/jpeg", "jpg"
    else:
        image = image.convert("RGBA")
        image.thumbnail(LOGO_MAX_SIZE, Image.Resampling.LANCZOS)
        image.save(output, format="PNG", optimize=True)
        content_type, extension = "image/png", "png"
    return ProcessedImage(output.getvalue(), content_type, extension, image.width, image.height)


def normalise_photo(data: bytes) -> ProcessedImage:
    """A profile photo as a square JPEG, cropped from the centre."""
    image = _flatten(_open(data)[0])
    image = ImageOps.fit(image, (PHOTO_SIZE, PHOTO_SIZE), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=85, optimize=True)
    return ProcessedImage(output.getvalue(), "image/jpeg", "jpg", PHOTO_SIZE, PHOTO_SIZE)
