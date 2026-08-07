#!/usr/bin/env python3
"""
generate_calibration_chart.py — 生成 Spectra 6 屏幕校准图（FPS6）。

校准图直接以设备 nibble 直写六种纯色（黑/白/黄/红/蓝/绿），不经量化、
不抖动，确保屏幕上每个色块就是该墨水本身的真实颜色。在真机上显示后，
正面垂直拍照，再用 tools/calibrate_profile.py（或管理台「屏幕校准」）
采样出每种墨水的实际观感颜色。

用法：
    python tools/generate_calibration_chart.py -o cal.fps6 --preview cal.png
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from app import calibration  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default="calibration.fps6")
    ap.add_argument("--preview", help="同时输出横放视角 PNG 预览")
    args = ap.parse_args()

    Path(args.output).write_bytes(calibration.chart_fps6())
    print(f"OK: {args.output} (1200x1600 竖屏数据，横放观看)")

    if args.preview:
        Path(args.preview).write_bytes(calibration.chart_png())
        print(f"preview: {args.preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
