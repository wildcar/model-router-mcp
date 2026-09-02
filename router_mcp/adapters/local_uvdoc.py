"""UVDoc — straightening a photographed page, computed on this host.

UVDoc (Verhoeven, Magne, Sorkine-Hornung, SIGGRAPH Asia 2023, Apache 2.0) looks
at a photograph of a sheet and predicts a *grid*: for every point of the flat
page it will produce, where on the photograph that point lies. The picture is
never redrawn — the original pixels are carried across that grid with
`cv2.remap`, so content, resolution and colours survive and the only loss is one
re-interpolation. Perspective from the shooting angle and the curl of the page
go away; lighting, shadows and focus stay as they were.

The weights are the `fredcallagan/uvdoc-grid-onnx` export of the original
checkpoint, chosen because it emits the grid rather than a finished 288×288
picture (the PaddlePaddle variant does the latter, and a recipe's text does not
survive 288 pixels). Fetched by `deploy/fetch-models.sh`, sha256-checked there.

Everything in this module runs in a worker thread of `adapters/local.py`; it
imports onnxruntime, OpenCV and numpy lazily so the router process pays for
them only once a request actually arrives.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path
from typing import Any

from router_mcp.domain.errors import ImageTooLarge, ProviderError

MODEL_ID = "local/uvdoc"
DISPLAY_NAME = "UVDoc"
MODEL_CARD = "https://huggingface.co/fredcallagan/uvdoc-grid-onnx"
LICENSE = "Apache-2.0"
# Relative to the models directory (`LOCAL_MODELS_DIR`). Two files: ONNX keeps
# the 31 MB of fp32 weights as external data next to the graph.
WEIGHT_FILES = ("uvdoc/UVDoc_grid.onnx", "uvdoc/UVDoc_grid.onnx.data")

INPUT_NAME = "image"
# The network's fixed input: RGB, 720 rows by 496 columns, values in [0, 1].
# Only the copy fed to the network is shrunk to this; the original is untouched.
NET_HEIGHT, NET_WIDTH = 720, 496
# The output: 2 × 45 × 31 values in [-1, 1] — an inverse map, x then y.
GRID_ROWS, GRID_COLS = 45, 31

# Above this the run is refused from the file header, before any pixel is
# decoded: three full-size buffers for a 50-megapixel frame are already a
# noticeable share of this host's memory, and past it the process would be
# killed rather than answer.
MAX_PIXELS = 50_000_000
JPEG_QUALITY = 92
PNG_COMPRESSION = 3
# `cv2.remap` works on horizontal bands of the output, so the coordinate maps
# never exist at full size: for a 12-megapixel photo that is 80 MB of float32
# that is simply not allocated. The band height is a memory/overhead knob only —
# the result is identical to one remap over the whole frame.
BAND_ROWS = 256

DESCRIPTION = (
    "Выпрямляет сфотографированную страницу: убирает перспективу от угла "
    "съёмки и изгиб листа; найденный лист растягивается на весь кадр, фон "
    "вокруг отрезается. Пиксели оригинала переносятся по предсказанной сетке, "
    "разрешение и цвета сохраняются. В кадре должен быть один лист, и его края "
    "должны быть видны хотя бы частично. Разворот книги из двух страниц, "
    "стопка листов, лист, обрезанный кадром с трёх сторон — результат "
    "непредсказуем. Освещение, тени и резкость не меняет. Если документа в "
    "кадре нет, модель всё равно «найдёт» лист и вернёт произвольно обрезанный "
    "и растянутый кадр, а не исходное фото — проверяйте, что на снимке "
    "документ. Текст не читает, параметров нет. Снимок больше 50 мегапикселей "
    "не берёт. Считается на процессоре роутера, бесплатно."
)


def load(models_dir: Path, threads: int) -> Any:
    """An ONNX Runtime session over the weights, CPU only.

    `threads` is the intra-op parallelism for this one session; the adapter
    divides the host's cores between the sessions it may run at once, so two
    concurrent runs do not oversubscribe a four-core box.
    """
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, int(threads))
    options.inter_op_num_threads = 1
    options.log_severity_level = 3  # errors only; warnings go to stderr otherwise
    return ort.InferenceSession(
        str(models_dir / WEIGHT_FILES[0]), options, providers=["CPUExecutionProvider"]
    )


def infer(
    session: Any, files: dict[str, bytes], params: dict[str, Any]
) -> tuple[bytes, str, dict[str, Any]]:
    """One page: bytes in, bytes out, plus the numbers that explain the run.

    `params` is accepted for the adapter's uniform signature and ignored: the
    model has no knobs, and the row's allowlist is empty accordingly.
    """
    data = files["image"]
    declared = image_dimensions(data)
    if declared is not None and declared[0] * declared[1] > MAX_PIXELS:
        raise ImageTooLarge(_too_large_message(*declared))

    import cv2
    import numpy as np

    t0 = time.perf_counter()
    # IMREAD_COLOR: always three channels, alpha dropped, EXIF orientation
    # applied — so a phone's sideways JPEG comes in upright. The encoder below
    # writes no metadata at all, which is how EXIF is stripped.
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ProviderError(
            "UVDoc could not decode the image (not a JPEG/PNG/WebP/BMP the "
            "decoder understands, or a truncated file)"
        )
    height, width = image.shape[:2]
    if height * width > MAX_PIXELS:
        raise ImageTooLarge(_too_large_message(width, height))
    if height < 2 or width < 2:
        raise ProviderError("UVDoc needs an image of at least 2×2 pixels")
    t1 = time.perf_counter()

    grid = predict_grid(session, image)
    t2 = time.perf_counter()
    unwarped = unwarp(image, grid)
    t3 = time.perf_counter()

    ext, mime = output_format(data)
    flags = (
        [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
        if ext == ".jpg"
        else [cv2.IMWRITE_PNG_COMPRESSION, PNG_COMPRESSION]
    )
    ok, encoded = cv2.imencode(ext, unwarped, flags)
    if not ok:
        raise ProviderError(f"UVDoc could not encode the result as {mime}")
    t4 = time.perf_counter()

    meta = {
        "input_size": [int(width), int(height)],
        "output_size": [int(unwarped.shape[1]), int(unwarped.shape[0])],
        "output_format": mime,
        "grid_range": [round(float(grid.min()), 4), round(float(grid.max()), 4)],
        "decode_ms": _ms(t0, t1),
        "predict_ms": _ms(t1, t2),
        "remap_ms": _ms(t2, t3),
        "encode_ms": _ms(t3, t4),
        "total_ms": _ms(t0, t4),
    }
    return encoded.tobytes(), mime, meta


def predict_grid(session: Any, image_bgr: Any) -> Any:
    """The inverse map, as `(GRID_ROWS, GRID_COLS, 2)` float32 in [-1, 1].

    The copy handed to the network is resized with plain bilinear sampling, as
    the authors' demo does; the original is not touched.
    """
    import cv2
    import numpy as np

    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    small = cv2.resize(rgb, (NET_WIDTH, NET_HEIGHT), interpolation=cv2.INTER_LINEAR)
    blob = np.ascontiguousarray(small.transpose(2, 0, 1)[None]).astype(np.float32) / 255.0
    out = session.run(None, {INPUT_NAME: blob})[0]  # (1, 2, 45, 31)
    return np.ascontiguousarray(out[0].transpose(1, 2, 0), dtype=np.float32)


def unwarp(image: Any, grid: Any) -> Any:
    """Carry the original pixels across the grid; same size out as in.

    The 45×31 grid is stretched bilinearly to the output size with the grid's
    corners pinned to the image corners (`align_corners=True`, as in the
    authors' `bilinear_unwarping`), the [-1, 1] coordinates become pixel
    positions in the full-resolution original, and `cv2.remap` samples them
    with linear interpolation. Done in horizontal bands so the two coordinate
    maps are never allocated at full size; the result is the same.
    """
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    rows, cols = grid.shape[:2]

    # Horizontal pass once: (rows, cols, 2) -> (rows, width, 2). Small.
    xs = np.linspace(0.0, cols - 1, width, dtype=np.float32)
    x0 = np.clip(np.floor(xs).astype(np.int32), 0, cols - 2)
    wx = (xs - x0)[None, :, None]
    wide = grid[:, x0] * (1.0 - wx) + grid[:, x0 + 1] * wx

    # Vertical pass per band: (band, width, 2), then the remap of that band.
    ys = np.linspace(0.0, rows - 1, height, dtype=np.float32)
    y0 = np.clip(np.floor(ys).astype(np.int32), 0, rows - 2)
    wy = ys - y0
    half_w = np.float32(0.5 * (width - 1))
    half_h = np.float32(0.5 * (height - 1))

    out = np.empty_like(image)
    for start in range(0, height, BAND_ROWS):
        stop = min(start + BAND_ROWS, height)
        band_y0 = y0[start:stop]
        band_wy = wy[start:stop][:, None, None]
        band = wide[band_y0] * (1.0 - band_wy) + wide[band_y0 + 1] * band_wy
        map_x = np.ascontiguousarray((band[..., 0] + 1.0) * half_w, dtype=np.float32)
        map_y = np.ascontiguousarray((band[..., 1] + 1.0) * half_h, dtype=np.float32)
        out[start:stop] = cv2.remap(
            image, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
        )
    return out


# --- pure helpers (the tests drive these without a model) --------------------
def output_format(data: bytes) -> tuple[str, str]:
    """The container the answer is written in: the input's own for JPEG and
    PNG, PNG for everything else (lossless, and never a guess about a codec
    the caller may not be able to read back)."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg", "image/jpeg"
    return ".png", "image/png"


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """`(width, height)` from the file header alone, or None when the format is
    not one we can read that way. Decoding to find out how big a picture is
    defeats the point of asking; these four containers state it up front."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data[:2] == b"BM" and len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        return abs(int(width)), abs(int(height))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_dimensions(data)
    if data[:3] == b"\xff\xd8\xff":
        return _jpeg_dimensions(data)
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk the marker segments to the first SOFn; its payload is the frame
    size. EXIF orientation may swap the two on decode, but the pixel count,
    which is what the cap is about, is the same either way."""
    pos = 2
    size = len(data)
    while pos + 4 <= size:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no payload
            pos += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image / start of scan: no SOF ahead
            return None
        if pos + 4 > size:
            return None
        length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        is_sof = 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC)
        if is_sof:
            if pos + 9 > size:
                return None
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return int(width), int(height)
        pos += 2 + length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    return None


def _too_large_message(width: int, height: int) -> str:
    return (
        f"Image is {width}×{height} = {width * height / 1e6:.1f} megapixels; "
        f"UVDoc on this host takes at most {MAX_PIXELS // 1_000_000}. "
        "Downscale the photo before sending it."
    )


def _ms(start: float, end: float) -> int:
    return int((end - start) * 1000)
