#!/usr/bin/env python3
"""
calibrate_profile.py — 从校准图照片采样 Spectra 6 六色真实观感。

流程：
  1. 真机显示 tools/generate_calibration_chart.py 生成的校准图（横放观看）
  2. 正面垂直拍摄一张照片（避免反光、尽量占满画面）
  3. 运行本工具：从照片定位 6 条色带的中心区域，取每带中位颜色
  4. 生成 services/app/profiles/calibrated.json，v2 调色板自动加载生效

采样规则（与 generate_calibration_chart.py 布局比例一致，横放视角 1600x1200）：
  - 每条色带中心 60% 宽 x 60% 高区域
  - 各通道取中位数（抗反光斑/噪点，比均值稳）

用法：
    python tools/calibrate_profile.py cal_photo.jpg [--preview out.png]
"""
import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from PIL import Image  # noqa: E402

from app.epd_image import CALIBRATED_PROFILE_PATH, SPECTRA6_PALETTE  # noqa: E402

# 与 generate_calibration_chart.py 布局对应（横放视角比例坐标）
VIEW_W, VIEW_H = 1600, 1200
BAND_H, BAND_GAP, BAND_TOP = 130, 14, 90
# 采样区：色带中右部（x 0.30..0.95 避开左端数字标签），高度为色带中央 60%
SX0, SX1 = 0.30, 0.95
SAMPLE_FRAC_Y = 0.6


def band_center_fracs() -> list[tuple[float, float, float, float]]:
    """返回每条色带采样区（x0,y0,x1,y1，比例 0..1）。"""
    out = []
    for i in range(6):
        top = BAND_TOP + i * (BAND_H + BAND_GAP)
        mid = top + BAND_H / 2
        h = BAND_H * SAMPLE_FRAC_Y
        y0 = (mid - h / 2) / VIEW_H
        y1 = (mid + h / 2) / VIEW_H
        out.append((SX0, y0, SX1, y1))
    return out


def median_color(img: Image.Image, x0, y0, x1, y1) -> tuple[int, int, int]:
    """区域各通道中位数（坐标已为绝对像素值）。"""
    rs, gs, bs = [], [], []
    px = img.load()
    w, h = int((x1 - x0)), int((y1 - y0))
    if w < 8 or h < 8:
        raise SystemExit("采样区太小，请确保照片中校准图尽量占满画面")
    for dy in range(h):
        for dx in range(w):
            r, g, b = px[x0 + dx, y0 + dy][:3]
            rs.append(r)
            gs.append(g)
            bs.append(b)
    return (round(statistics.median(rs)), round(statistics.median(gs)), round(statistics.median(bs)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("photo", help="校准图照片路径")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                    help="照片中校准图方向不对时旋转（逆时针，Pillow 正角）。"
                         "默认 0：校准图内容在照片中横放（宽>高）。")
    ap.add_argument("--preview", help="在照片上画出采样框并输出标注图")
    ap.add_argument("--no-write", action="store_true", help="只打印结果，不写 calibrated.json")
    args = ap.parse_args()

    img = Image.open(args.photo).convert("RGB")
    if args.rotate:
        img = img.rotate(args.rotate, expand=True)
        print(f"rotated {args.rotate}deg -> {img.size}")
    print(f"photo: {img.size}")

    regions = band_center_fracs()
    device = []
    for i, (x0, y0, x1, y1) in enumerate(regions):
        c = median_color(img, int(x0 * img.width), int(y0 * img.height),
                         int(x1 * img.width), int(y1 * img.height))
        device.append(list(c))
        print(f"  band {i} {SPECTRA6_PALETTE[i]!s:>18} -> 实拍 {tuple(c)}")

    if args.preview:
        from PIL import ImageDraw, ImageFont
        marked = img.copy()
        d = ImageDraw.Draw(marked)
        f = ImageFont.load_default(24)
        for i, (x0, y0, x1, y1) in enumerate(regions):
            box = (int(x0 * img.width), int(y0 * img.height),
                   int(x1 * img.width), int(y1 * img.height))
            d.rectangle(box, outline=(255, 0, 0), width=6)
            d.text((box[0] + 4, box[1] + 4), f"band {i}", fill=(255, 0, 0), font=f)
        marked.save(args.preview)
        print(f"marked preview: {args.preview}（红色框 = 各色带采样区）")

    payload = {
        "profile": "v2",
        "device": device,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "source_chart": "tools/generate_calibration_chart.py (1600x1200 landscape)",
        "note": "device 色顺序与 NIBBLES 一致：[黑,白,黄,红,蓝,绿]",
    }
    if not args.no_write:
        CALIBRATED_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATED_PROFILE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                                           encoding="utf-8")
        print(f"\nwritten: {CALIBRATED_PROFILE_PATH}")
    else:
        print("\n(--no-write) 未写入文件，以上为本次采样结果")
    return 0


if __name__ == "__main__":
    sys.exit(main())
