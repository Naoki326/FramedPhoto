"""
epd_image.py — 为 E Ink Spectra 6 屏幕准备图像。

流程：
  1. 解码输入图片（Pillow）
  2. 按屏幕纵横比缩放到 EPD_WIDTH x EPD_HEIGHT（居中裁剪保持比例）
  3. 量化到 Spectra 6 色板（6 色），Floyd–Steinberg 抖动补偿色阶损失
  4. 输出"索引图"：每像素 1 字节（0..5 = 色板下标），设备端直接映射到显存

输出格式（自描述二进制头，共 20 字节 + W*H 字节索引数据）：
  offset 0 : "FPS6" magic (4B)
  offset 4 : width  u32 LE
  offset 8 : height u32 LE
  offset 12: format u8 (0 = 索引图)
  offset 13: palette_version u8 (当前 1)
  offset 14: reserved 6B (置 0)
  offset 20: W*H 字节像素索引
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass

from PIL import Image

MAGIC = b"FPS6"
FORMAT_INDEX = 0
PALETTE_VERSION = 1
HEADER_SIZE = 20

# Spectra 6 六色：黑、白、红、绿、蓝、橙（近似 sRGB 值，最终以数据手册色域为准）
SPECTRA6_PALETTE: list[tuple[int, int, int]] = [
    (0, 0, 0),          # 0 黑
    (255, 255, 255),    # 1 白
    (255, 34, 34),      # 2 红
    (0, 150, 60),       # 3 绿
    (0, 70, 200),       # 4 蓝
    (255, 150, 0),      # 5 橙
]
PALETTE_INDEX = {color: i for i, color in enumerate(SPECTRA6_PALETTE)}


@dataclass
class PreparedImage:
    width: int
    height: int
    data: bytes            # 索引图字节流（含头）
    palette: list[tuple[int, int, int]]

    @property
    def raw_index(self) -> bytes:
        return self.data[HEADER_SIZE:]


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    """按 cover 模式缩放：铺满目标尺寸并居中裁剪，保持比例不失真。"""
    img = image.convert("RGB")
    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return img.crop((left, top, left + width, top + height))


def _nearest_index(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    best, best_d = 0, float("inf")
    for i, (pr, pg, pb) in enumerate(SPECTRA6_PALETTE):
        d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
        if d < best_d:
            best, best_d = i, d
    return best


def _floyd_steinberg(pixels: list[list[tuple[int, int, int]]]) -> list[list[int]]:
    """在 6 色板上做 Floyd–Steinberg 抖动，返回每像素色板下标。"""
    h, w = len(pixels), len(pixels[0])
    err: list[list[tuple[float, float, float]]] = [
        [(0.0, 0.0, 0.0)] * w for _ in range(h)
    ]
    out: list[list[int]] = [[0] * w for _ in range(h)]

    for y in range(h):
        for x in range(w):
            r, g, b = pixels[y][x]
            er, eg, eb = err[y][x]
            r2, g2, b2 = max(0, min(255, r + er)), max(0, min(255, g + eg)), max(0, min(255, b + eb))
            idx = _nearest_index((r2, g2, b2))
            out[y][x] = idx
            pr, pg, pb = SPECTRA6_PALETTE[idx]
            qr, qg, qb = (r2 - pr), (g2 - pg), (b2 - pb)
            # 误差扩散到右 / 下 / 左下 / 右下
            if x + 1 < w:
                err[y][x + 1] = _add(err[y][x + 1], (qr * 7 / 16, qg * 7 / 16, qb * 7 / 16))
            if y + 1 < h:
                err[y + 1][x] = _add(err[y + 1][x], (qr * 5 / 16, qg * 5 / 16, qb * 5 / 16))
                if x > 0:
                    err[y + 1][x - 1] = _add(err[y + 1][x - 1], (qr * 3 / 16, qg * 3 / 16, qb * 3 / 16))
                if x + 1 < w:
                    err[y + 1][x + 1] = _add(err[y + 1][x + 1], (qr * 1 / 16, qg * 1 / 16, qb * 1 / 16))
    return out


def _add(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def prepare_image(
    image_bytes: bytes,
    width: int = 1200,
    height: int = 1600,
    dither: bool = True,
) -> PreparedImage:
    """输入任意格式图片字节，输出 FramedPhoto 索引图。"""
    img = Image.open(io.BytesIO(image_bytes))
    img = _fit(img, width, height)

    if dither:
        pix = list(img.getdata())
        rows = [pix[y * width:(y + 1) * width] for y in range(height)]
        indices = _floyd_steinberg(rows)
    else:
        pix = list(img.getdata())
        indices = [[_nearest_index(pix[y * width + x]) for x in range(width)] for y in range(height)]

    raw = bytes(idx for row in indices for idx in row)
    header = struct.pack(
        "<4sIIBB6x", MAGIC, width, height, FORMAT_INDEX, PALETTE_VERSION
    )
    return PreparedImage(
        width=width,
        height=height,
        data=header + raw,
        palette=SPECTRA6_PALETTE,
    )


def preview_png(prepared: PreparedImage) -> bytes:
    """把索引图还原成 PNG 预览（方便用户在服务端网页查看转换效果）。"""
    raw = prepared.raw_index
    img = Image.new("RGB", (prepared.width, prepared.height))
    img.putdata([SPECTRA6_PALETTE[i] for i in raw])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
