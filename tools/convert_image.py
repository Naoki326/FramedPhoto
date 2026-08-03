#!/usr/bin/env python3
"""
convert_image.py — 命令行图片转换（复用服务端 epd_image 模块）。

用法：
    python tools/convert_image.py input.jpg -o out.fps6 --width 1200 --height 1600
    python tools/convert_image.py input.jpg -o out.fps6 --no-dither
    python tools/convert_image.py input.jpg --preview out.png   # 输出转换效果预览

输出：FramedPhoto 索引图（FPS6 格式），供固件 / 服务端使用。
"""
import argparse
import sys
from pathlib import Path

# 让脚本能 import 服务端模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from app.epd_image import prepare_image, preview_png  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="输入图片路径")
    ap.add_argument("-o", "--output", help="输出 .fps6 文件路径")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=1600)
    ap.add_argument("--no-dither", action="store_true", help="关闭抖动")
    ap.add_argument("--preview", help="同时输出 PNG 预览到此路径")
    args = ap.parse_args()

    raw = Path(args.input).read_bytes()
    p = prepare_image(raw, args.width, args.height, dither=not args.no_dither)

    out = Path(args.output or f"{Path(args.input).stem}.fps6")
    out.write_bytes(p.data)
    print(f"OK: {out} ({p.width}x{p.height}, {len(p.data)} bytes)")

    if args.preview:
        Path(args.preview).write_bytes(preview_png(p))
        print(f"preview: {args.preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
