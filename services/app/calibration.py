"""calibration.py — Spectra 6 屏幕校准核心逻辑（服务端 / tools 共用）。

- build_chart(): 生成校准图（横放视角 1600x1200 RGB，仅含 6 色），
  to_fps6(): 转 FPS6 竖屏数据（nibble 直写，绕过量化）
- sample_photo(): 从校准照片采样六色真实观感
- save_calibrated(): 写 calibrated.json 并热加载 v2 profile
"""
from __future__ import annotations

import io
import json
import statistics
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.epd_image import (
    CALIBRATED_PROFILE_PATH,
    DEVICE_HEIGHT,
    DEVICE_WIDTH,
    NIBBLES,
    SPECTRA6_PALETTE,
    find_cjk_font,
    reload_calibrated_profile,
)

# 横放视角画布（设备横放观看时正立）
VIEW_W, VIEW_H = DEVICE_HEIGHT, DEVICE_WIDTH   # 1600x1200

# 布局参数（横放视角坐标）
TITLE_H = 70
BAND_H = 130
BAND_GAP = 14
BAND_TOP = 90
BAND_LABELS = ["1", "2", "3", "4", "5", "6"]
# 每带的标签颜色（黑带白字，白带黑字，亮黄黑字，其余白字）
BAND_TEXT_COLOR = [(255, 255, 255), (0, 0, 0), (0, 0, 0),
                   (255, 255, 255), (255, 255, 255), (255, 255, 255)]

# 采样区：色带中右部（x 0.30..0.95 避开左端数字标签），高度为色带中央 60%
SX0, SX1 = 0.30, 0.95
SAMPLE_FRAC_Y = 0.6

# 默认占位 device 色（未校准时使用，黑白为占位）
DEFAULT_DEVICE = [
    (0, 0, 0),           # 0x0 黑
    (255, 255, 255),     # 0x1 白
    (149, 151, 50),      # 0x2 黄
    (109, 21, 17),       # 0x3 红
    (33, 65, 127),       # 0x5 蓝
    (86, 120, 95),       # 0x6 绿
]

COLOR_NAMES = ["黑", "白", "黄", "红", "蓝", "绿"]


# ---------- 校准图生成 ----------

def _nearest_palette_index(rgb: tuple[int, int, int]) -> int:
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

    # 六色带 + 数字标签（标签贴左端，采样区为带中右部，互不干扰）
    for i, color in enumerate(SPECTRA6_PALETTE):
        top = BAND_TOP + i * (BAND_H + BAND_GAP)
        draw.rectangle([24, top, VIEW_W - 24, top + BAND_H], fill=color)
        label = BAND_LABELS[i]
        th = 92
        draw.text((60, top + (BAND_H - th) / 2), label,
                  font=font_label, fill=BAND_TEXT_COLOR[i])

    # 底部：黑白 2x2 棋盘（抖动/均匀性观察）
    CHESS_TOP = BAND_TOP + 6 * (BAND_H + BAND_GAP) + 8
    cell = 16
    for gy in range(12):
        for gx in range(40):
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


def chart_png() -> bytes:
    buf = io.BytesIO()
    build_chart().save(buf, format="PNG")
    return buf.getvalue()


def chart_fps6() -> bytes:
    """横放视角画布 -> FPS6 竖屏数据（与 prepare_image_with_caption 一致）。"""
    canvas = build_chart().rotate(90, expand=True)
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
    header = (b"FPS6" + width.to_bytes(4, "little") + height.to_bytes(4, "little")
              + bytes([1, 1]) + b"\x00" * 6)
    return header + bytes(buf)


# ---------- 照片采样 ----------

def band_center_fracs() -> list[tuple[float, float, float, float]]:
    """每条色带采样区（x0,y0,x1,y1，比例 0..1，横放视角）。"""
    out = []
    for i in range(6):
        top = BAND_TOP + i * (BAND_H + BAND_GAP)
        mid = top + BAND_H / 2
        h = BAND_H * SAMPLE_FRAC_Y
        y0 = (mid - h / 2) / VIEW_H
        y1 = (mid + h / 2) / VIEW_H
        out.append((SX0, y0, SX1, y1))
    return out


def sample_photo(img: Image.Image) -> list[list[int]]:
    """从校准图照片采样六色（各通道中位数，抗反光斑）。"""
    img = img.convert("RGB")
    device = []
    for x0, y0, x1, y1 in band_center_fracs():
        x0i, y0i = int(x0 * img.width), int(y0 * img.height)
        x1i, y1i = int(x1 * img.width), int(y1 * img.height)
        w, h = x1i - x0i, y1i - y0i
        if w < 8 or h < 8:
            raise ValueError("采样区太小：请确保照片中校准图尽量占满画面")
        rs, gs, bs = [], [], []
        px = img.load()
        for dy in range(h):
            for dx in range(w):
                r, g, b = px[x0i + dx, y0i + dy][:3]
                rs.append(r)
                gs.append(g)
                bs.append(b)
        device.append([round(statistics.median(rs)),
                       round(statistics.median(gs)),
                       round(statistics.median(bs))])
    return device


def save_calibrated(device: list[list[int]]) -> dict:
    """写 calibrated.json 并热加载 v2 profile，返回写入内容。"""
    payload = {
        "profile": "v2",
        "device": device,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "source_chart": "app.calibration (1600x1200 landscape)",
        "note": "device 色顺序与 NIBBLES 一致：[黑,白,黄,红,蓝,绿]",
    }
    CALIBRATED_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATED_PROFILE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    reload_calibrated_profile()
    return payload


def clear_calibrated() -> bool:
    """删除 calibrated.json 并恢复默认占位；返回是否有文件被删除。"""
    existed = CALIBRATED_PROFILE_PATH.exists()
    if existed:
        CALIBRATED_PROFILE_PATH.unlink()
    reload_calibrated_profile()
    return existed


def calibration_status() -> dict:
    """当前校准状态（供管理台展示）。"""
    from app.epd_image import SPECTRA6_PROFILE_V2
    calibrated = CALIBRATED_PROFILE_PATH.exists()
    try:
        meta = json.loads(CALIBRATED_PROFILE_PATH.read_text("utf-8"))
        captured = meta.get("captured_at", "")
    except (OSError, ValueError):
        captured = ""
    return {
        "calibrated": calibrated,
        "captured_at": captured,
        "device": [list(c) for c in SPECTRA6_PROFILE_V2.device],
        "names": COLOR_NAMES,
        "defaults": [list(c) for c in DEFAULT_DEVICE],
    }


# ---------- 彩虹效果图（视觉校准辅助） ----------
# 与校准图不同，彩虹图走「正常量化链路」（v2 profile + 抖动），
# 用户看到的就是照片/内容在设备上的真实转换效果，便于整体评估与逐色匹配。


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h % 360 / 360.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return (round(r * 255), round(g * 255), round(b * 255))


def build_rainbow_chart() -> Image.Image:
    """彩虹效果图（横放视角 1600x1200）：三条色相渐变带 + 底部六纯色块。

    仅含理想 6 色与 sRGB 渐变，经正常量化后上屏。
    """
    import colorsys as _cs
    canvas = Image.new("RGB", (VIEW_W, VIEW_H), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    font_path = find_cjk_font()
    font_title = ImageFont.truetype(font_path, 36) if font_path else ImageFont.load_default(36)

    draw.text((24, 18), "E Ink Spectra6 彩虹效果图（正常转换链路，底部为六纯色参考）",
              font=font_title, fill=(0, 0, 0))

    # 三条色相渐变带：亮 / 深 / 淡
    bands = [(90, 210, 1.0, 1.0), (300, 210, 0.55, 1.0), (510, 210, 1.0, 0.5)]
    for top, hgt, sat, val in bands:
        for x in range(VIEW_W):
            hue = x / VIEW_W * 360
            color = _hsv_to_rgb(hue, sat, val)
            draw.line([(x, top), (x, top + hgt)], fill=color)

    # 底部六纯色块（1..6，与校准图一致）
    font_label = ImageFont.truetype(font_path, 64) if font_path else ImageFont.load_default(64)
    block_top = 760
    block_h = 170
    n = 6
    bw = (VIEW_W - 48) // n
    for i, color in enumerate(SPECTRA6_PALETTE):
        x0 = 24 + i * bw
        draw.rectangle([x0, block_top, x0 + bw - 8, block_top + block_h], fill=color)
        label = str(i + 1)
        tw = draw.textlength(label, font=font_label)
        draw.text((x0 + (bw - tw) / 2, block_top + (block_h - 64) / 2), label,
                  font=font_label, fill=BAND_TEXT_COLOR[i])

    draw.text((24, 960), "顶部三条彩虹带为抖动混色效果；点击下方色块区域可查看对应编号。",
              font=font_title, fill=(0, 0, 0))
    return canvas


def rainbow_png() -> bytes:
    buf = io.BytesIO()
    build_rainbow_chart().save(buf, format="PNG")
    return buf.getvalue()


def rainbow_fps6() -> bytes:
    """彩虹图经正常转换链路（v2 量化+抖动）输出 FPS6。"""
    from app.epd_image import prepare_image
    return prepare_image(rainbow_png(), profile="v2").data
