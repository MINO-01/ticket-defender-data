"""Validate image bytes before any model adapter can receive them."""

import asyncio
import base64
import binascii
from dataclasses import dataclass
import hashlib
from io import BytesIO
import threading
import warnings

from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from core.vlm_errors import VLMProblem


MAX_PIXELS = 20_000_000
MAX_DIMENSION = 12_000
MAX_ESTIMATED_DECODED_BYTES = 64 * 1024 * 1024
LEGACY_MAX_BASE64_LENGTH = 5_242_880
LEGACY_MAX_IMAGE_BYTES = LEGACY_MAX_BASE64_LENGTH // 4 * 3
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
_VALIDATION_SLOT = threading.BoundedSemaphore(1)
_VALIDATION_WAIT_SECONDS = 0.05


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    mime_type: str
    sha256: str
    width: int
    height: int


def _release_validation_slot(completed: asyncio.Task):
    # A cancelled request must not release its slot while Pillow is still running.
    try:
        if not completed.cancelled():
            completed.exception()
    finally:
        _VALIDATION_SLOT.release()


async def validate_image_in_thread(
    encoded: str, claimed_mime: str, *, max_base64_length: int,
    max_bytes: int, expected_sha256: str | None = None, allow_webp: bool = False,
) -> ValidatedImage:
    """Bound worker validation and retain the slot until the worker actually exits."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _VALIDATION_WAIT_SECONDS
    while not _VALIDATION_SLOT.acquire(blocking=False):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise VLMProblem(503, "IMAGE_VALIDATION_BUSY", "Analysis busy", "Image validation capacity is exhausted")
        await asyncio.sleep(min(0.005, remaining))

    try:
        task = asyncio.create_task(run_in_threadpool(
            validate_image, encoded, claimed_mime,
            max_base64_length=max_base64_length, max_bytes=max_bytes,
            expected_sha256=expected_sha256, allow_webp=allow_webp,
        ))
    except BaseException:
        _VALIDATION_SLOT.release()
        raise
    task.add_done_callback(_release_validation_slot)
    return await asyncio.shield(task)


def validate_image(
    encoded: str, claimed_mime: str, *, max_base64_length: int,
    max_bytes: int, expected_sha256: str | None = None, allow_webp: bool = False,
) -> ValidatedImage:
    if len(encoded) > max_base64_length:
        raise VLMProblem(413, "IMAGE_TOO_LARGE", "Image too large", "Image exceeds the encoded limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise VLMProblem(400, "INVALID_BASE64", "Invalid image", "Invalid Base64 image") from None
    if base64.b64encode(data).decode("ascii") != encoded:
        raise VLMProblem(400, "INVALID_BASE64", "Invalid image", "Invalid Base64 image")
    if len(data) > max_bytes:
        raise VLMProblem(413, "IMAGE_TOO_LARGE", "Image too large", "Image exceeds the byte limit")
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise VLMProblem(400, "IMAGE_SHA256_MISMATCH", "Invalid image", "Image digest mismatch")

    formats = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    allowed = {"image/jpeg", "image/png", "image/webp"} if allow_webp else {"image/jpeg", "image/png"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                actual = formats.get(image.format)
                if actual is None or actual not in allowed:
                    raise VLMProblem(415, "UNSUPPORTED_IMAGE_TYPE", "Unsupported image", "Unsupported image format")
                if actual != claimed_mime:
                    raise VLMProblem(415, "IMAGE_MIME_MISMATCH", "Invalid image", "Image MIME does not match bytes")
                width, height = image.size
                if width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
                    raise VLMProblem(413, "IMAGE_TOO_LARGE", "Image too large", "Image exceeds the pixel limit")
                bytes_per_pixel = {"1": 1, "L": 1, "P": 1, "LA": 2, "RGB": 3, "RGBA": 4, "CMYK": 4}.get(image.mode)
                if bytes_per_pixel is None:
                    raise VLMProblem(422, "INVALID_IMAGE", "Invalid image", "Unsupported image color mode")
                if width * height * bytes_per_pixel > MAX_ESTIMATED_DECODED_BYTES:
                    raise VLMProblem(413, "IMAGE_TOO_LARGE", "Image too large", "Image exceeds the decoded memory limit")
                if getattr(image, "n_frames", 1) != 1:
                    raise VLMProblem(422, "INVALID_IMAGE", "Invalid image", "Animated images are unsupported")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
    except VLMProblem:
        raise
    except Image.DecompressionBombWarning:
        raise VLMProblem(413, "IMAGE_TOO_LARGE", "Image too large", "Image exceeds the pixel limit") from None
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise VLMProblem(422, "INVALID_IMAGE", "Invalid image", "Image cannot be decoded") from None
    return ValidatedImage(data, claimed_mime, digest, width, height)
