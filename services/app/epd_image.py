"""
epd_image.py — 为 E Ink Spectra 6 屏幕准备图像。

流程：
  1. 解码输入图片（Pillow）
  2. 按屏幕纵横比缩放到 1200x1600（居中裁剪保持比例）
  3. 量化到 Spectra 6 色板（6 色），Floyd–Steinberg 抖动补偿色阶损失
  4. 封装为 FPS6 设备格式（4bit/像素，双 IC 布局），设备端直接转发到驱动

设备格式（FPS6 v1, format=1）：
  - 头 20 字节：magic "FPS6" | width u32LE | height u32LE | format u8 | palette_version u8 | reserved 6B
  - 数据区 960000 字节（1200*1600/2）
  - 每行 600 字节：前 300B 送 IC0（右半屏），后 300B 送 IC1（左半屏）
  - 行内像素从右到左：字节 k 对应逻辑像素 (1198-2k, 1197-2k)，高 nibble 为偶数像素
  - 颜色 nibble：黑 0x0 白 0x1 黄 0x2 红 0x3 蓝 0x5 绿 0x6（与官方驱动一致）

与固件对接：firmware/components/gdey_epd/ 的 gdeb0709e01_display_image()
直接接受该数据区；逐行读 600B 即可（前 300B IC0、后 300B IC1）。
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass

from PIL import Image

MAGIC = b"FPS6"
FORMAT_4BIT_DUAL_IC = 1
PALETTE_VERSION = 1
HEADER_SIZE = 20

DEVICE_WIDTH = 1200
DEVICE_HEIGHT = 1600

# 设备颜色 nibble（官方 GDEP133C02.h：BLACK 0x0 WHITE 0x1 YELLOW 0x2 RED 0x3 BLUE 0x5 GREEN 0x6）
NIBBLES = (0x0, 0x1, 0x2, 0x3, 0x5, 0x6)
NIBBLE_TO_INDEX = {n: i for i, n in enumerate(NIBBLES)}

# Spectra 6 六色近似 sRGB —— 顺序与设备 nibble 一一对应（索引 i -> NIBBLES[i]）
SPECTRA6_PALETTE: list[tuple[int, int, int]] = [
    (0, 0, 0),          # 0x0 黑
    (255, 255, 255),    # 0x1 白
    (255, 150, 0),      # 0x2 黄（设备黄偏橙）
    (255, 34, 34),      # 0x3 红
    (0, 70, 200),       # 0x5 蓝
    (0, 150, 60),       # 0x6 绿
]


@dataclass
class PreparedImage:
    width: int
    height: int
    data: bytes            # FPS6 完整字节流（含头）
    palette: list[tuple[int, int, int]]

    @property
    def raw_index(self) -> bytes:
        """数据区：960000 字节设备布局。"""
        return self.data[HEADER_SIZE:]


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    """cover 缩放：铺满目标尺寸并居中裁剪，保持比例不失真。"""
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
    """6 色板 Floyd–Steinberg 抖动，返回每像素色板索引 0..5。"""
    h, w = len(pixels), len(pixels[0])
    err: list[list[tuple[float, float, float]]] = [[(0.0, 0.0, 0.0)] * w for _ in range(h)]
    out: list[list[int]] = [[0] * w for _ in range(h)]

    for y in range(h):
        for x in range(w):
            r, g, b = pixels[y][x]
            er, eg, eb = err[y][x]
            r2 = max(0, min(255, r + er))
            g2 = max(0, min(255, g + eg))
            b2 = max(0, min(255, b + eb))
            idx = _nearest_index((r2, g2, b2))
            out[y][x] = idx
            pr, pg, pb = SPECTRA6_PALETTE[idx]
            qr, qg, qb = (r2 - pr), (g2 - pg), (b2 - pb)
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


def _to_device_layout(indices: list[list[int]], width: int) -> bytes:
    """6 色索引矩阵 -> FPS6 设备布局字节流。

    布局：每行 width/2 字节；字节 k 对应像素对 (width-2-2k, width-1-2k)；
    高 nibble = 偶数像素，低 nibble = 奇数像素；nibble 值经 NIBBLES 映射。
    """
    row_bytes = width // 2
    buf = bytearray(width * len(indices) // 2)
    for y, row in enumerate(indices):
        base = y * row_bytes
        for x in range(0, width, 2):
            k = (width - 2 - x) // 2
            hi = NIBBLES[row[x]]
            lo = NIBBLES[row[x + 1]]
            buf[base + k] = (hi << 4) | lo
    return bytes(buf)


def prepare_image(
    image_bytes: bytes,
    width: int = DEVICE_WIDTH,
    height: int = DEVICE_HEIGHT,
    dither: bool = True,
) -> PreparedImage:
    """输入任意格式图片字节，输出 FPS6 设备格式。"""
    if (width, height) != (DEVICE_WIDTH, DEVICE_HEIGHT):
        raise ValueError(f"only {DEVICE_WIDTH}x{DEVICE_HEIGHT} supported, got {width}x{height}")

    img = Image.open(io.BytesIO(image_bytes))
    img = _fit(img, width, height)

    pix = list(img.getdata())
    if dither:
        rows = [pix[y * width:(y + 1) * width] for y in range(height)]
        indices = _floyd_steinberg(rows)
    else:
        indices = [[_nearest_index(pix[y * width + x]) for x in range(width)] for y in range(height)]

    raw = _to_device_layout(indices, width)
    header = struct.pack(
        "<4sIIBB6x", MAGIC, width, height, FORMAT_4BIT_DUAL_IC, PALETTE_VERSION
    )
    return PreparedImage(width=width, height=height, data=header + raw, palette=SPECTRA6_PALETTE)


# 字节值 -> (偶数像素 RGB, 奇数像素 RGB) 的 256 项查找表（预览用）
# 非法 nibble（如 0x4/0x7+）在合法数据中不应出现，兜底映射为黑
_BYTE_LOOKUP = [
    (
        SPECTRA6_PALETTE[NIBBLE_TO_INDEX.get(b >> 4, 0)],
        SPECTRA6_PALETTE[NIBBLE_TO_INDEX.get(b & 0xF, 0)],
    )
    for b in range(256)
]


def preview_png(prepared: PreparedImage) -> bytes:
    """把 FPS6 数据区还原成 PNG 预览（Web/CLI 查看转换效果）。"""
    raw = prepared.raw_index
    row_bytes = prepared.width // 2
    img = Image.new("RGB", (prepared.width, prepared.height))
    px = img.load()
    for y in range(prepared.height):
        base = y * row_bytes
        for k in range(row_bytes):
            even_rgb, odd_rgb = _BYTE_LOOKUP[raw[base + k]]
            x = prepared.width - 2 - 2 * k
            px[x, y] = even_rgb
            px[x + 1, y] = odd_rgb
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
