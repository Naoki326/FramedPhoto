#!/usr/bin/env python3
"""
convert_image.py — 命令行图片转换（复用服务端 epd_image 模块）。

输出 FPS6 设备格式（4bit 双 IC 布局），可直接喂给固件 gdeb0709e01_display_image()。

用法：
    python tools/convert_image.py input.jpg -o out.fps6
    python tools/convert_image.py input.jpg --no-dither
    python tools/convert_image.py input.jpg --preview out.png   # 输出转换效果预览
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
    ap.add_argument("--no-dither", action="store_true", help="关闭抖动")
    ap.add_argument("--profile", default="v2", choices=["v1", "v2"],
                    help="调色板：v1=历史理想色；v2=校准色空间（默认，墨水对混色）")
    ap.add_argument("--chroma-bias", type=float, default=None,
                    help="浓彩偏置 0~4（默认读服务端 runtime_config；0=忠实混色，越大越浓彩）")
    ap.add_argument("--preview", help="同时输出 PNG 预览到此路径")
    args = ap.parse_args()

    raw = Path(args.input).read_bytes()
    p = prepare_image(raw, dither=not args.no_dither, profile=args.profile,
                      chroma_bias=args.chroma_bias)

    out = Path(args.output or f"{Path(args.input).stem}.fps6")
    out.write_bytes(p.data)
    print(f"OK: {out} ({p.width}x{p.height}, {len(p.data)} bytes)")

    if args.preview:
        Path(args.preview).write_bytes(preview_png(p))
        print(f"preview: {args.preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
