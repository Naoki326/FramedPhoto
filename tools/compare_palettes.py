#!/usr/bin/env python3
"""
compare_palettes.py — v1 / v2 调色板 A/B 对比。

对同一张输入图：
  - v1：历史行为（RGB 距离 + 理想色渲染预览）
  - v2：OKLab 感知距离 + 校准观感色渲染预览（贴近真机）
输出对比拼图（横放视角）：原图缩略 | v1 理想预览 | v2 真机观感预览，
并打印两种方案的墨水使用占比。

用法：
    python tools/compare_palettes.py input.jpg -o /tmp/ab/ --name 对比
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from PIL import Image  # noqa: E402

from app.epd_image import (  # noqa: E402
    SPECTRA6_PROFILE_V1,
    NIBBLE_TO_INDEX,
    get_profile,
    is_landscape,
    prepare_image,
    preview_png_landscape,
)

LABELS = ["黑", "白", "黄", "红", "蓝", "绿"]


def usage_stats(prepared) -> dict[str, float]:
    """统计各墨水占比（%）。"""
    counts = [0] * 6
    raw = prepared.raw_index
    for b in raw:
        counts[NIBBLE_TO_INDEX[b >> 4]] += 1
        counts[NIBBLE_TO_INDEX[b & 0xF]] += 1
    total = prepared.width * prepared.height
    return {LABELS[i]: round(100 * counts[i] / total, 2) for i in range(6)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="输入图片路径")
    ap.add_argument("-o", "--outdir", default="ab_compare", help="输出目录")
    ap.add_argument("--name", default="compare", help="输出文件名前缀")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    raw = Path(args.input).read_bytes()
    landscape = is_landscape(raw)

    variants = {"v1": get_profile("v1"), "v2": get_profile("v2")}
    prepared = {}
    for name in variants:
        p = prepare_image(raw, dither=True, profile=name)
        prepared[name] = p
        (outdir / f"{args.name}-{name}.fps6").write_bytes(p.data)
        img = Image.open(__import__("io").BytesIO(preview_png_landscape(p, landscape)))
        img.save(outdir / f"{args.name}-{name}-preview.png")
        print(f"[{name}] FPS6 -> {outdir / (args.name + '-' + name + '.fps6')}")
        print(f"         预览 -> {outdir / (args.name + '-' + name + '-preview.png')}")
        print(f"         墨水占比: {usage_stats(p)}")

    # 拼图：原图 | v1 | v2
    src = Image.open(args.input).convert("RGB")
    if landscape:
        src = src.rotate(90, expand=True)
    src.thumbnail((640, 480))
    v1_img = Image.open(outdir / f"{args.name}-v1-preview.png")
    v2_img = Image.open(outdir / f"{args.name}-v2-preview.png")
    v1_img.thumbnail((640, 480))
    v2_img.thumbnail((640, 480))

    H = max(src.height, v1_img.height, v2_img.height)
    W = src.width + v1_img.width + v2_img.width
    grid = Image.new("RGB", (W, H), (255, 255, 255))
    for im, x in ((src, 0), (v1_img, src.width), (v2_img, src.width + v1_img.width)):
        grid.paste(im, (x, (H - im.height) // 2))
    grid_path = outdir / f"{args.name}-grid.png"
    grid.save(grid_path)
    print(f"对比拼图（左:原图 中:v1 右:v2真机观感）-> {grid_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
