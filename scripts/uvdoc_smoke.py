#!/usr/bin/env python3
"""Smoke run of the local UVDoc model on real photographs, with timings.

Not a test: it writes the straightened pictures next to the inputs so a human
can look at them, and prints the numbers that go into the task's closing note —
how long each stage took on this host's CPU, how far the predicted grid is from
"leave it alone", and how much the picture changed. The three photos the task
asks for are a sheet shot at an angle, a curved book page, and an ordinary
photograph with no paper in it; the last should come back almost untouched.

    . .venv/bin/activate
    python scripts/uvdoc_smoke.py photo1.jpg photo2.jpg photo3.jpg --out /tmp/uvdoc
    python scripts/uvdoc_smoke.py --synthetic 4000x3000 --out /tmp/uvdoc   # timing only

Uses the same `load()` / `infer()` the router does (`adapters/local_uvdoc.py`),
so what is measured is what a run would do. Weights: deploy/fetch-models.sh.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from router_mcp.adapters import local_uvdoc  # noqa: E402
from router_mcp.config import load_settings  # noqa: E402


def synthetic_page(width: int, height: int, seed: int = 0):
    """A dark desk, a light page covered in text-like bars, shot at an angle and
    curled — so the straightening has something to straighten and the run has
    a known size to be timed at."""
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    desk = (58, 66, 74)
    page_h, page_w = int(height * 0.82), int(width * 0.74)
    page = np.full((page_h, page_w, 3), 236, np.uint8)
    line_h = max(4, page_h // 48)
    y = int(page_h * 0.08)
    while y < page_h * 0.92:
        x = int(page_w * 0.08)
        while x < page_w * 0.9:
            w = int(rng.integers(int(page_w * 0.03), int(page_w * 0.12)))
            cv2.rectangle(page, (x, y), (min(x + w, int(page_w * 0.9)), y + line_h), (24, 24, 24), -1)
            x += w + int(page_w * 0.02)
        y += int(line_h * 2.3)
    src = np.float32([[0, 0], [page_w, 0], [page_w, page_h], [0, page_h]])
    dst = np.float32([
        [width * 0.20, height * 0.14], [width * 0.84, height * 0.05],
        [width * 0.93, height * 0.90], [width * 0.09, height * 0.96],
    ])
    matrix = cv2.getPerspectiveTransform(src, dst)
    shot = cv2.warpPerspective(
        page, matrix, (width, height), borderMode=cv2.BORDER_CONSTANT, borderValue=desk
    )
    # A gentle curl: rows bend along a half sine across the width.
    xs = np.arange(width, dtype=np.float32)
    ys = np.arange(height, dtype=np.float32)
    map_x = np.tile(xs, (height, 1))
    map_y = ys[:, None] + (height * 0.04) * np.sin(xs / width * np.pi)[None, :]
    return cv2.remap(shot, map_x, map_y.astype(np.float32), cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=desk)


def identity_deviation(session, data: bytes) -> float:
    """Mean distance of the predicted grid from the grid that changes nothing,
    in units of the [-1, 1] coordinate range. Near zero means "no document
    here, leave it"; a real page shot at an angle sits well above 0.05."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    grid = local_uvdoc.predict_grid(session, image)
    rows, cols = grid.shape[:2]
    identity = np.stack(
        np.meshgrid(np.linspace(-1, 1, cols), np.linspace(-1, 1, rows)), axis=-1
    ).astype(np.float32)
    return float(np.abs(grid - identity).mean())


def mean_abs_diff(a: bytes, b: bytes) -> float:
    import cv2
    import numpy as np

    x = cv2.imdecode(np.frombuffer(a, np.uint8), cv2.IMREAD_COLOR).astype(np.int16)
    y = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR).astype(np.int16)
    return float(np.abs(x - y).mean())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images", nargs="*", help="photographs to straighten")
    parser.add_argument("--out", default="smoke-out", help="where to write the results")
    parser.add_argument("--models-dir", default=None, help="default: LOCAL_MODELS_DIR")
    parser.add_argument("--threads", type=int, default=None,
                        help="ONNX intra-op threads; default: cores // LOCAL_MAX_CONCURRENCY")
    parser.add_argument("--synthetic", metavar="WxH", default=None,
                        help="also run a generated warped page of this size, for timing")
    parser.add_argument("--repeat", type=int, default=1, help="runs per image; the fastest is reported")
    args = parser.parse_args()

    settings = load_settings()
    models_dir = Path(args.models_dir or settings.local_models_dir)
    threads = args.threads or max(1, (os.cpu_count() or 1) // settings.local_max_concurrency)
    missing = [f for f in local_uvdoc.WEIGHT_FILES if not (models_dir / f).is_file()]
    if missing:
        print(f"weights missing under {models_dir}: {', '.join(missing)} — run deploy/fetch-models.sh", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    t = time.perf_counter()
    session = local_uvdoc.load(models_dir, threads)
    print(f"model loaded in {int((time.perf_counter() - t) * 1000)} ms, threads={threads}, cpu={os.cpu_count()}")

    jobs: list[tuple[str, bytes]] = []
    for path in args.images:
        jobs.append((Path(path).name, Path(path).read_bytes()))
    if args.synthetic:
        import cv2

        width, height = (int(v) for v in args.synthetic.lower().split("x"))
        ok, buf = cv2.imencode(".jpg", synthetic_page(width, height), [cv2.IMWRITE_JPEG_QUALITY, 92])
        assert ok
        jobs.append((f"synthetic_{width}x{height}.jpg", buf.tobytes()))
    if not jobs:
        parser.error("give at least one image or --synthetic WxH")

    print(f"{'image':<32} {'size':>11} {'decode':>7} {'predict':>8} {'remap':>7} {'encode':>7} {'total':>7}  {'grid dev':>8} {'px diff':>7}")
    for name, data in jobs:
        best = None
        for _ in range(max(1, args.repeat)):
            result, mime, meta = local_uvdoc.infer(session, {"image": data}, {})
            if best is None or meta["total_ms"] < best[2]["total_ms"]:
                best = (result, mime, meta)
        result, mime, meta = best
        ext = ".jpg" if mime == "image/jpeg" else ".png"
        target = out_dir / (Path(name).stem + ".unwarped" + ext)
        target.write_bytes(result)
        w, h = meta["input_size"]
        deviation = identity_deviation(session, data)
        diff = mean_abs_diff(data, result)
        print(
            f"{name[:32]:<32} {w:>5}x{h:<5} {meta['decode_ms']:>5} ms {meta['predict_ms']:>5} ms "
            f"{meta['remap_ms']:>5} ms {meta['encode_ms']:>5} ms {meta['total_ms']:>5} ms  "
            f"{deviation:>8.3f} {diff:>7.1f}   -> {target}"
        )
    print("grid dev: mean |grid - identity| in [-1,1] units (≈0 = nothing to straighten); "
          "px diff: mean |out - in| per channel, 0..255")
    return 0


if __name__ == "__main__":
    sys.exit(main())
