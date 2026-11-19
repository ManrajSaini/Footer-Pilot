"""Storage backends, image normalisation and the time-ago filter. No network."""

import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from pydantic import ValidationError

from app.config import Settings
from app.services import images
from app.services.images import InvalidImageError, normalise_photo
from app.storage import build_storage
from app.storage.local import LocalStorage
from app.storage.r2 import CACHE_CONTROL, R2Storage
from app.web.templating import timeago
from tests.fakes import image_bytes

R2 = {
    "storage_backend": "r2",
    "r2_account_id": "abc123",
    "r2_access_key_id": "key-id",
    "r2_secret_access_key": "secret",
    "r2_bucket": "footerpilot-assets",
    "r2_public_base_url": "https://assets.example.com/",
}


# --- Local ----------------------------------------------------------------------------


async def test_local_storage_writes_the_file_and_returns_its_url(tmp_path):
    storage = LocalStorage(tmp_path / "media", "http://testserver/media/")
    url = await storage.put("orgs/1/photos/abc.jpg", b"jpeg-bytes", "image/jpeg")
    assert url == "http://testserver/media/orgs/1/photos/abc.jpg"
    assert (tmp_path / "media" / "orgs" / "1" / "photos" / "abc.jpg").read_bytes() == b"jpeg-bytes"


@pytest.mark.parametrize("key", ["../outside.txt", "orgs/../../outside.txt", "/etc/passwd"])
async def test_local_storage_refuses_keys_outside_its_folder(tmp_path, key):
    storage = LocalStorage(tmp_path / "media", "http://testserver/media")
    with pytest.raises(ValueError, match="escapes"):
        await storage.put(key, b"x", "text/plain")
    assert not (tmp_path / "outside.txt").exists()


# --- R2 -------------------------------------------------------------------------------


class RecordingS3Client:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_object(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {}


@pytest.fixture
def s3(monkeypatch):
    client = RecordingS3Client()
    created: dict = {}

    def fake_client(service, **kwargs):
        created.update(service=service, **kwargs)
        return client

    monkeypatch.setattr("app.storage.r2.boto3.client", fake_client)
    client.created = created
    return client


async def test_r2_storage_uploads_with_the_right_settings(s3):
    storage = build_storage(Settings(_env_file=None, **R2))
    assert isinstance(storage, R2Storage)
    assert s3.created["service"] == "s3"
    assert s3.created["endpoint_url"] == "https://abc123.r2.cloudflarestorage.com"
    assert s3.created["aws_access_key_id"] == "key-id"
    assert s3.created["aws_secret_access_key"] == "secret"

    url = await storage.put("orgs/1/photos/abc.jpg", b"jpeg-bytes", "image/jpeg")
    assert url == "https://assets.example.com/orgs/1/photos/abc.jpg"  # no double slash
    assert s3.calls == [
        {
            "Bucket": "footerpilot-assets",
            "Key": "orgs/1/photos/abc.jpg",
            "Body": b"jpeg-bytes",
            "ContentType": "image/jpeg",
            "CacheControl": CACHE_CONTROL,
        }
    ]


def test_local_backend_is_the_default(tmp_path):
    storage = build_storage(Settings(_env_file=None, media_root=str(tmp_path / "m")))
    assert isinstance(storage, LocalStorage)
    assert storage.url("a/b.png") == "http://testserver/media/a/b.png"


@pytest.mark.parametrize(
    "missing", ["r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket"]
)
def test_r2_backend_requires_all_its_settings(missing):
    with pytest.raises(ValidationError, match=missing.upper()):
        Settings(_env_file=None, **{**R2, missing: ""})


# --- Images ---------------------------------------------------------------------------


def test_photo_becomes_a_square_jpeg():
    result = normalise_photo(image_bytes("blue", (640, 480)))
    image = Image.open(io.BytesIO(result.data))
    assert (image.format, image.size, image.mode) == ("JPEG", (160, 160), "RGB")
    assert (result.content_type, result.extension) == ("image/jpeg", "jpg")
    assert (result.width, result.height) == (160, 160)


def test_photo_is_cropped_from_the_centre_not_squashed():
    # Left half red, right half blue, in a wide image.
    wide = Image.new("RGB", (400, 100), "red")
    wide.paste(Image.new("RGB", (200, 100), "blue"), (200, 0))
    output = io.BytesIO()
    wide.save(output, format="PNG")

    image = Image.open(io.BytesIO(normalise_photo(output.getvalue()).data))
    left, right = image.getpixel((10, 80)), image.getpixel((150, 80))
    assert left[0] > 200 and left[2] < 60  # still red on the left
    assert right[2] > 200 and right[0] < 60  # still blue on the right


def test_transparency_is_placed_on_white():
    transparent = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    output = io.BytesIO()
    transparent.save(output, format="PNG")
    image = Image.open(io.BytesIO(normalise_photo(output.getvalue()).data))
    assert all(channel > 245 for channel in image.getpixel((80, 80)))


def test_metadata_is_not_carried_over():
    source = Image.new("RGB", (200, 200), "green")
    exif = Image.Exif()
    exif[0x010E] = "secret description"  # ImageDescription
    output = io.BytesIO()
    source.save(output, format="JPEG", exif=exif)
    assert b"secret description" in output.getvalue()

    assert b"secret description" not in normalise_photo(output.getvalue()).data


def test_same_image_gives_the_same_digest():
    data = image_bytes("purple")
    assert normalise_photo(data).digest == normalise_photo(data).digest
    assert normalise_photo(data).digest != normalise_photo(image_bytes("orange")).digest
    assert len(normalise_photo(data).digest) == 20


@pytest.mark.parametrize(
    "data",
    [b"", b"not an image", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", b"\x89PNG\r\n"],
)
def test_unreadable_data_is_rejected(data):
    with pytest.raises(InvalidImageError):
        normalise_photo(data)


def test_oversized_image_is_rejected(monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 100)
    with pytest.raises(InvalidImageError, match="too large"):
        normalise_photo(image_bytes(size=(50, 50)))


# --- Time ago -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=5), "just now"),
        (timedelta(seconds=61), "1 minute ago"),
        (timedelta(minutes=42), "42 minutes ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(hours=23, minutes=59), "23 hours ago"),
        (timedelta(days=3), "on 29 Dec 2025"),
    ],
)
def test_timeago(delta, expected):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert timeago(now - delta, now) == expected


def test_timeago_without_a_value():
    assert timeago(None) == "never"
