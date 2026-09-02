"""UVDoc's own code: the header sniffing that guards memory, the container
choice, and — when the weights are installed — the geometry of the remap.

The header tests need nothing installed. The geometry tests skip themselves
without onnxruntime/OpenCV or without the weights (`deploy/fetch-models.sh`),
which is the state of a fresh checkout; on the host that serves the model they
run.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from router_mcp.adapters import local_uvdoc
from router_mcp.config import load_settings
from router_mcp.domain.errors import ImageTooLarge


def _png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    )


def _jpeg_header(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xda"


# --- headers ---------------------------------------------------------------------
def test_dimensions_are_read_from_png_jpeg_bmp_and_webp_headers():
    assert local_uvdoc.image_dimensions(_png_header(640, 480)) == (640, 480)
    assert local_uvdoc.image_dimensions(_jpeg_header(4032, 3024)) == (4032, 3024)
    bmp = b"BM" + b"\x00" * 16 + struct.pack("<ii", 800, -600) + b"\x00" * 10
    assert local_uvdoc.image_dimensions(bmp) == (800, 600)
    vp8x = (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + b"\x00" * 4 + b"\x00" * 4
            + (1023).to_bytes(3, "little") + (767).to_bytes(3, "little"))
    assert local_uvdoc.image_dimensions(vp8x) == (1024, 768)
    assert local_uvdoc.image_dimensions(b"GIF89a....") is None
    assert local_uvdoc.image_dimensions(b"") is None


def test_a_frame_above_the_cap_is_refused_from_the_header_alone():
    """The whole point: the refusal happens before any pixel exists in memory,
    so no decoder is even imported for it."""
    with pytest.raises(ImageTooLarge) as info:
        local_uvdoc.infer(None, {"image": _png_header(9000, 7000)}, {})
    assert info.value.code == "image_too_large"
    assert info.value.http_status == 413
    assert info.value.retriable is False
    assert "63.0 megapixels" in str(info.value)


def test_the_answer_keeps_the_inputs_container_for_jpeg_and_png_only():
    assert local_uvdoc.output_format(_jpeg_header(1, 1)) == (".jpg", "image/jpeg")
    assert local_uvdoc.output_format(_png_header(1, 1)) == (".png", "image/png")
    assert local_uvdoc.output_format(b"RIFF....WEBP") == (".png", "image/png")


# --- geometry, with the real libraries -----------------------------------------------
def _weights() -> Path | None:
    root = Path(load_settings().local_models_dir)
    if all((root / f).is_file() for f in local_uvdoc.WEIGHT_FILES):
        return root
    return None


@pytest.fixture(scope="module")
def np():
    return pytest.importorskip("numpy")


@pytest.fixture(scope="module")
def cv2():
    return pytest.importorskip("cv2")


def _page(cv2, np, width: int = 800, height: int = 1000):
    """A light page with dark horizontal bars, seen straight on."""
    img = np.full((height, width, 3), 235, np.uint8)
    for y in range(80, height - 80, 40):
        cv2.rectangle(img, (60, y), (width - 60, y + 12), (20, 20, 20), -1)
    return img


def test_an_identity_grid_leaves_the_picture_alone(cv2, np):
    """Pins the coordinate convention: corners of the grid are the corners of
    the image (`align_corners=True`), so a grid that says "everything is where
    it is" must reproduce the input to within a rounding step."""
    rng = np.random.default_rng(1)
    image = rng.integers(0, 255, size=(301, 203, 3), dtype=np.uint8)
    rows, cols = local_uvdoc.GRID_ROWS, local_uvdoc.GRID_COLS
    grid = np.stack(
        np.meshgrid(np.linspace(-1, 1, cols), np.linspace(-1, 1, rows)), axis=-1
    ).astype(np.float32)
    out = local_uvdoc.unwarp(image, grid)
    assert out.shape == image.shape
    assert int(np.abs(out.astype(int) - image.astype(int)).max()) <= 1


def test_bands_give_the_same_picture_as_one_remap(cv2, np, monkeypatch):
    """The banding exists for memory only; it must not change a pixel."""
    rng = np.random.default_rng(2)
    image = rng.integers(0, 255, size=(700, 500, 3), dtype=np.uint8)
    grid = (rng.random((local_uvdoc.GRID_ROWS, local_uvdoc.GRID_COLS, 2)) * 1.6 - 0.8).astype(np.float32)
    banded = local_uvdoc.unwarp(image, grid)
    monkeypatch.setattr(local_uvdoc, "BAND_ROWS", 10_000)
    whole = local_uvdoc.unwarp(image, grid)
    assert np.array_equal(banded, whole)


def test_a_photographed_page_comes_back_the_same_size_and_container(cv2, np):
    root = _weights()
    if root is None:
        pytest.skip("UVDoc weights not installed (deploy/fetch-models.sh)")
    session = local_uvdoc.load(root, threads=2)

    # The straight page, then a perspective shot of it on a dark desk.
    page = _page(cv2, np)
    height, width = 1100, 900
    src = np.float32([[0, 0], [800, 0], [800, 1000], [0, 1000]])
    dst = np.float32([[200, 120], [780, 60], [860, 1000], [90, 1060]])
    shot = cv2.warpPerspective(
        page, cv2.getPerspectiveTransform(src, dst), (width, height),
        borderMode=cv2.BORDER_CONSTANT, borderValue=(60, 66, 74),
    )
    ok, jpeg = cv2.imencode(".jpg", shot, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok

    data, mime, meta = local_uvdoc.infer(session, {"image": jpeg.tobytes()}, {})
    assert mime == "image/jpeg"
    assert data[:3] == b"\xff\xd8\xff"
    out = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert out.shape == shot.shape
    assert meta["input_size"] == [width, height] == meta["output_size"]
    assert meta["predict_ms"] > 0 and meta["total_ms"] >= meta["predict_ms"]
    # The grid is a real prediction, not the identity: something was straightened.
    lo, hi = meta["grid_range"]
    assert -1.0 <= lo < hi <= 1.0

    ok, png = cv2.imencode(".png", shot)
    data, mime, _ = local_uvdoc.infer(session, {"image": png.tobytes()}, {})
    assert mime == "image/png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_output_carries_no_exif(cv2, np):
    """The encoder writes pixels only; whatever metadata came in stays out."""
    root = _weights()
    if root is None:
        pytest.skip("UVDoc weights not installed (deploy/fetch-models.sh)")
    session = local_uvdoc.load(root, threads=2)
    ok, jpeg = cv2.imencode(".jpg", _page(cv2, np, 400, 500))
    raw = jpeg.tobytes()
    # Splice an APP1/Exif segment in after SOI.
    exif = b"Exif\x00\x00MM\x00\x2a\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00"
    with_exif = raw[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + raw[2:]
    assert b"Exif" in with_exif
    data, _, _ = local_uvdoc.infer(session, {"image": with_exif}, {})
    assert b"Exif" not in data
