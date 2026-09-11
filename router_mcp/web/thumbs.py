"""Small previews of stored images, for the gallery and the entry window.

A generated picture is one to two megabytes of PNG; a gallery page shows forty
of them, and the entry window shows the ones a row carried next to its number.
Serving the originals there means sixty megabytes for a screen of thumbnails,
so `GET /blobs/{ref}/thumb` answers with a copy no wider than `MAX_SIDE`.

Made on demand with OpenCV when it is installed (it comes with the `local`
extra, which production has) and never stored: the blob store's retention and
orphan sweep know one file per ref, and a second file per ref would be a second
thing for them to know. Without OpenCV, or for a format it cannot read, the
original is served instead — slower, never wrong.
"""

from __future__ import annotations

from collections import OrderedDict

try:  # pragma: no cover - which branch runs depends on the install
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None
    np = None

MAX_SIDE = 320
JPEG_QUALITY = 82

# A few hundred thumbnails at ~20 KB each: the gallery's first pages, warm
# after the first visitor. Bounded, so a scroll through the whole log does not
# turn into the whole log held in memory.
_CACHE_ENTRIES = 300
_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()


def available() -> bool:
    return cv2 is not None


def make(data: bytes, max_side: int = MAX_SIDE) -> tuple[bytes, str] | None:
    """(bytes, mime) of a preview, or None when the original should be served:
    no decoder, an unreadable file, a depth JPEG cannot hold, or a picture that
    is already small enough to be its own thumbnail."""
    if cv2 is None:
        return None
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.dtype != np.uint8:
        return None
    height, width = image.shape[:2]
    scale = max_side / max(height, width, 1)
    if scale >= 1:
        return None
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    small = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    # Transparency survives only in PNG; everything else is a smaller JPEG.
    if small.ndim == 3 and small.shape[2] == 4:
        ok, buffer = cv2.imencode(".png", small)
        mime = "image/png"
    else:
        ok, buffer = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        mime = "image/jpeg"
    if not ok:
        return None
    return buffer.tobytes(), mime


def cached(ref: str) -> tuple[bytes, str] | None:
    hit = _cache.get(ref)
    if hit is not None:
        _cache.move_to_end(ref)
    return hit


def remember(ref: str, thumb: tuple[bytes, str]) -> None:
    _cache[ref] = thumb
    _cache.move_to_end(ref)
    while len(_cache) > _CACHE_ENTRIES:
        _cache.popitem(last=False)
