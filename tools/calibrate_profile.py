#!/usr/bin/env python3
"""
calibrate_profile.py — 从校准图照片采样 Spectra 6 六色真实观感。

流程：
  1. 真机显示 tools/generate_calibration_chart.py 生成的校准图（横放观看）
  2. 正面垂直拍摄一张照片（避免反光、尽量占满画面）
  3. 运行本工具：从照片定位 6 条色带的中心区域，取每带中位颜色
  4. 生成 services/app/profiles/calibrated.json，v2 调色板立即生效
     （管理台「屏幕校准」提供同功能的网页操作）

用法：
    python tools/calibrate_profile.py cal_photo.jpg [--preview marked.png]
    python tools/calibrate_profile.py cal_photo.jpg --no-write   # 只打印
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app import calibration  # noqa: E402
from app.epd_image import SPECTRA6_PALETTE  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("photo", help="校准图照片路径")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270],
                    help="照片中校准图方向不对时旋转（逆时针，Pillow 正角）。默认 0。")
    ap.add_argument("--preview", help="在照片上画出采样框并输出标注图")
    ap.add_argument("--no-write", action="store_true", help="只打印结果，不写 calibrated.json")
    args = ap.parse_args()

    img = Image.open(args.photo)
    if args.rotate:
        img = img.rotate(args.rotate, expand=True)
        print(f"rotated {args.rotate}deg -> {img.size}")
    print(f"photo: {img.size}")

    device = calibration.sample_photo(img)
    for i, c in enumerate(device):
        print(f"  band {i} {str(SPECTRA6_PALETTE[i]):>18} -> 实拍 {tuple(c)}")

    if args.preview:
        marked = img.convert("RGB").copy()
        d = ImageDraw.Draw(marked)
        f = ImageFont.load_default(24)
        for i, (x0, y0, x1, y1) in enumerate(calibration.band_center_fracs()):
            box = (int(x0 * marked.width), int(y0 * marked.height),
                   int(x1 * marked.width), int(y1 * marked.height))
            d.rectangle(box, outline=(255, 0, 0), width=6)
            d.text((box[0] + 4, box[1] + 4), f"band {i}", fill=(255, 0, 0), font=f)
        marked.save(args.preview)
        print(f"marked preview: {args.preview}（红色框 = 各色带采样区）")

    if args.no_write:
        print("\n(--no-write) 未写入文件，以上为本次采样结果")
    else:
        payload = calibration.save_calibrated(device)
        print(f"\nwritten: services/app/profiles/calibrated.json（v2 已热加载生效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
