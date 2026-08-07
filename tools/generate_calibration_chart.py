#!/usr/bin/env python3
"""
generate_calibration_chart.py — 生成 Spectra 6 屏幕校准图（FPS6）。

校准图直接以设备 nibble 直写六种纯色（黑/白/黄/红/蓝/绿），不经量化、
不抖动，确保屏幕上每个色块就是该墨水本身的真实颜色。在真机上显示后，
正面垂直拍照，再运行 tools/calibrate_profile.py 从照片采样出每种墨水的
实际观感颜色，生成 calibrated.json 供 v2 调色板使用。

布局（横放视角 1600x1200，设备横放观看时正立）：
  - 顶部标题带（白底黑字）
  - 6 条纯色带：黑 1 / 白 2 / 黄 3 / 红 4 / 蓝 5 / 绿 6，中央大数字标记
  - 底部：黑白 2x2 棋盘（观察抖动/刷新均匀性）+ 灰阶纹理说明

用法：
    python tools/generate_calibration_chart.py -o cal.fps6 --preview cal.png
"""
import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.epd_image import (  # noqa: E402
    DEVICE_WIDTH,
    DEVICE_HEIGHT,
    NIBBLES,
    SPECTRA6_PALETTE,
    find_cjk_font,
)

VIEW_W, VIEW_H = DEVICE_HEIGHT, DEVICE_WIDTH   # 横放视角画布 1600x1200

# 布局参数（横放视角坐标）
TITLE_H = 70
BAND_H = 130
BAND_GAP = 14
BAND_TOP = 90
BAND_LABELS = ["1", "2", "3", "4", "5", "6"]
# 每带的标签颜色（黑带白字，白带黑字，亮黄黑字，其余白字）
BAND_TEXT_COLOR = [(255, 255, 255), (0, 0, 0), (0, 0, 0), (255, 255, 255), (255, 255, 255), (255, 255, 255)]


def _nearest_palette_index(rgb) -> int:
    """RGB -> 6 理想色最近索引（校准图只用 6 色绘制，映射无损）。"""
    r, g, b = rgb
    best, best_d = 0, float("inf")
    for i, (pr, pg, pb) in enumerate(SPECTRA6_PALETTE):
        d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
        if d < best_d:
            best, best_d = i, d
    return best


def build_chart() -> Image.Image:
    """绘制横放视角校准图（1600x1200 RGB，仅含 6 色）。"""
    canvas = Image.new("RGB", (VIEW_W, VIEW_H), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    font_path = find_cjk_font()
    font_label = ImageFont.truetype(font_path, 92) if font_path else ImageFont.load_default(92)
    font_title = ImageFont.truetype(font_path, 40) if font_path else ImageFont.load_default(40)

    # 顶部标题
    draw.text((24, (TITLE_H - 40) // 2), "E Ink Spectra6 Calibration — 正面垂直拍摄",
              font=font_title, fill=(0, 0, 0))

    # 六色带 + 数字标签（标签贴左端，校准采样区为带中右部，互不干扰）
    for i, color in enumerate(SPECTRA6_PALETTE):
        top = BAND_TOP + i * (BAND_H + BAND_GAP)
        draw.rectangle([24, top, VIEW_W - 24, top + BAND_H], fill=color)
        label = BAND_LABELS[i]
        tw = draw.textlength(label, font=font_label)
        th = 92
        draw.text((60, top + (BAND_H - th) / 2), label,
                  font=font_label, fill=BAND_TEXT_COLOR[i])

    # 底部：黑白 2x2 棋盘（抖动/均匀性观察）
    CHESS_TOP = BAND_TOP + 6 * (BAND_H + BAND_GAP) + 8
    cell = 16
    for gy in range(12):
        for gx in range(40):
            c = (0, 0, 0) if (gy // 1 + gx // 1) % 2 == 0 else (255, 255, 255)
            # 2x2 像素重复 -> 真正的 50% 黑白棋盘
            for dy in range(2):
                for dx in range(2):
                    if ((gx * 2 + dx) + (gy * 2 + dy)) % 2 == 0:
                        draw.rectangle([24 + gx * cell + dx * (cell // 2),
                                        CHESS_TOP + gy * cell + dy * (cell // 2),
                                        24 + gx * cell + dx * (cell // 2) + cell // 2,
                                        CHESS_TOP + gy * cell + dy * (cell // 2) + cell // 2],
                                       fill=(0, 0, 0))
    draw.text((24 + 40 * cell + 40, CHESS_TOP + 60), "50% 黑白棋盘",
              font=font_title, fill=(0, 0, 0))
    return canvas


def to_fps6(canvas: Image.Image) -> bytes:
    """横放视角画布 -> FPS6 竖屏数据（与 prepare_image_with_caption 一致）。"""
    canvas = canvas.rotate(90, expand=True)   # 回到设备竖屏 1200x1600
    width, height = canvas.size
    pix = list(canvas.getdata())
    indices = [_nearest_palette_index(p) for p in pix]

    row_bytes = width // 2
    buf = bytearray(width * height // 2)
    for y in range(height):
        base = y * row_bytes
        for x in range(0, width, 2):
            k = x // 2
            hi = NIBBLES[indices[y * width + x]]
            lo = NIBBLES[indices[y * width + x + 1]]
            buf[base + k] = (hi << 4) | lo
    header = b"FPS6" + (width).to_bytes(4, "little") + (height).to_bytes(4, "little") \
        + bytes([1, 1]) + b"\x00" * 6
    return header + bytes(buf)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default="calibration.fps6")
    ap.add_argument("--preview", help="同时输出横放视角 PNG 预览")
    args = ap.parse_args()

    canvas = build_chart()
    Path(args.output).write_bytes(to_fps6(canvas))
    print(f"OK: {args.output} ({DEVICE_WIDTH}x{DEVICE_HEIGHT} 竖屏数据，横放观看)")

    if args.preview:
        Path(args.preview).write_bytes(canvas_to_png(canvas))
        print(f"preview: {args.preview}")
    return 0


def canvas_to_png(canvas: Image.Image) -> bytes:
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    sys.exit(main())
