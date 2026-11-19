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


def _open(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            raise InvalidImageError("image is too large")
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise InvalidImageError("not a readable image") from error
    return ImageOps.exif_transpose(image)


def _flatten(image: Image.Image) -> Image.Image:
    """RGB with any transparency placed on white (JPEG has no alpha channel)."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def normalise_photo(data: bytes) -> ProcessedImage:
    """A profile photo as a square JPEG, cropped from the centre."""
    image = _flatten(_open(data))
    image = ImageOps.fit(image, (PHOTO_SIZE, PHOTO_SIZE), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=85, optimize=True)
    return ProcessedImage(output.getvalue(), "image/jpeg", "jpg", PHOTO_SIZE, PHOTO_SIZE)
