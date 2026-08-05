"""
epd_image.py — 为 E Ink Spectra 6 屏幕准备图像。

流程：
  1. 解码输入图片（Pillow），按 EXIF 方向摆正（手机照片）
  2. 适配到 1200x1600：竖屏/方形图 cover（居中裁剪铺满）；横屏图旋转 90° 后
     cover 铺满（相框顺时针横放观看，照片正立、无黑边，文字随照片一起旋转）
  3. 量化到 Spectra 6 色板（6 色），Floyd–Steinberg 抖动补偿色阶损失
  4. 封装为 FPS6 设备格式（4bit/像素，双 IC 布局），设备端直接转发到驱动

设备格式（FPS6 v1, format=1）：
  - 头 20 字节：magic "FPS6" | width u32LE | height u32LE | format u8 | palette_version u8 | reserved 6B
  - 数据区 960000 字节（1200*1600/2）
  - 每行 600 字节：前 300B 送 IC0（左半屏），后 300B 送 IC1（右半屏）
  - 行内像素从左到右：字节 k 对应逻辑像素 (2k, 2k+1)，高 nibble 为左像素
  - 颜色 nibble：黑 0x0 白 0x1 黄 0x2 红 0x3 蓝 0x5 绿 0x6（与官方驱动一致）

注意：早期版本把行内像素按“从右到左”打包（对应官方示例的水平扫描方向），
但实测 GDEB0709E01 双 IC 的扫描方向为从左到右、IC0 实际控制左半屏，
RTL 打包会导致整图水平镜像。2026-08 已在真机验证：LTR 打包显示正常。

与固件对接：firmware/components/gdey_epd/ 的 gdeb0709e01_display_image()
直接接受该数据区；逐行读 600B 即可（前 300B IC0、后 300B IC1）。
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

MAGIC = b"FPS6"
FORMAT_4BIT_DUAL_IC = 1
PALETTE_VERSION = 1
HEADER_SIZE = 20

DEVICE_WIDTH = 1200
DEVICE_HEIGHT = 1600

# 横屏照片旋转角度（Pillow 正角 = 逆时针）：旋转后 cover 铺满竖屏，
# 用户把相框顺时针横放（顶部向右）即可正立观看，文字随照片一起旋转。
ROTATE_LANDSCAPE = 90

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


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """cover 缩放：铺满目标尺寸并居中裁剪，保持比例不失真。"""
    img = image.convert("RGB")
    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return img.crop((left, top, left + width, top + height))


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    """适配目标尺寸，保持比例不失真。

    - 横屏图（宽 > 高）：逆时针旋转 90° 后 cover 铺满——相框顺时针横放观看，
      照片正立、整屏利用，文字随照片一起旋转（见 prepare_image_with_caption）；
    - 竖屏/方形图：cover，铺满并居中裁剪。

    适配前先按 EXIF 方向摆正（手机照片常见），否则横竖屏判断会出错。
    """
    img = ImageOps.exif_transpose(image)
    if img.size[0] > img.size[1]:
        img = img.rotate(ROTATE_LANDSCAPE, expand=True)
    return _cover(img, width, height)


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

    布局：每行 width/2 字节；字节 k 对应像素对 (2k, 2k+1)（从左到右）；
    高 nibble = 左像素，低 nibble = 右像素；nibble 值经 NIBBLES 映射。
    """
    row_bytes = width // 2
    buf = bytearray(width * len(indices) // 2)
    for y, row in enumerate(indices):
        base = y * row_bytes
        for x in range(0, width, 2):
            k = x // 2
            hi = NIBBLES[row[x]]
            lo = NIBBLES[row[x + 1]]
            buf[base + k] = (hi << 4) | lo
    return bytes(buf)


def _quantize_to_layout(img: Image.Image, width: int, height: int, dither: bool) -> bytes:
    """RGB 图像（已适配目标尺寸）-> FPS6 数据区。"""
    pix = list(img.getdata())
    if dither:
        rows = [pix[y * width:(y + 1) * width] for y in range(height)]
        indices = _floyd_steinberg(rows)
    else:
        indices = [[_nearest_index(pix[y * width + x]) for x in range(width)] for y in range(height)]
    return _to_device_layout(indices, width)


def _pack_fps6(raw: bytes, width: int, height: int) -> bytes:
    header = struct.pack(
        "<4sIIBB6x", MAGIC, width, height, FORMAT_4BIT_DUAL_IC, PALETTE_VERSION
    )
    return header + raw


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

    raw = _quantize_to_layout(img, width, height, dither)
    return PreparedImage(width=width, height=height, data=_pack_fps6(raw, width, height),
                         palette=SPECTRA6_PALETTE)


# ---------- 文案渲染（每日精选） ----------

# 跨平台中文字体候选
_CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]


def find_cjk_font() -> str | None:
    for p in _CJK_FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _wrap_cjk(draw, text: str, font, max_width: int) -> list[str]:
    """按像素宽度对中文文案换行。"""
    lines, cur = [], ""
    for ch in text:
        trial = cur + ch
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _draw_caption(canvas: Image.Image, width: int, height: int,
                  caption: str, date_str: str) -> None:
    """在画布底部画圆角深色衬底块 + 日期小字 + 文案白字（正立，先画后量化）。

    不遮挡照片：每个文字块按文字实际尺寸自适应，其余区域照片透出。
    无中文字体或文案为空时不应调用本函数。
    """
    from PIL import ImageDraw, ImageFont

    font_path = find_cjk_font()
    if not font_path:
        return

    draw = ImageDraw.Draw(canvas)
    margin = int(width * 0.06)
    max_text_w = width - margin * 2
    PAD = int(height * 0.024)     # 文字与衬底内边距
    CORNER = int(height * 0.020)  # 圆角半径
    BAND = (22, 22, 26)           # 深色衬底（E Ink 近似黑）

    # 底部对齐：从下往上摆块，日期块（若有）+ 文案块
    blocks: list[tuple[int, int, str]] = []   # (font_size, text_lines, text)
    small = big = None
    if date_str:
        small = ImageFont.truetype(font_path, int(height * 0.030))
        blocks.append((small, [date_str]))
    if caption.strip():
        big = ImageFont.truetype(font_path, int(height * 0.045))
        lines = _wrap_cjk(draw, caption.strip(), big, max_text_w - 2 * PAD)[:2]
        if lines:
            blocks.append((big, lines))
    if not blocks:
        return

    # 自底向上计算每个块的位置
    y = height - margin
    for font, lines in reversed(blocks):
        bw = max(draw.textlength(l, font=font) for l in lines)
        bh = len(lines) * int(height * 0.058)
        block_bottom = y
        block_top = block_bottom - bh - 2 * PAD
        bx = (width - bw) / 2 - PAD
        draw.rounded_rectangle([bx, block_top, bx + bw + 2 * PAD, block_bottom],
                               radius=CORNER, fill=BAND)
        ty = block_top + PAD
        for line in lines:
            lw = draw.textlength(line, font=font)
            draw.text(((width - lw) / 2, ty), line, font=font, fill=(255, 255, 255))
            ty += int(height * 0.058)
        y = block_top - int(height * 0.030)   # 块间距



def prepare_image_with_caption(
    image_bytes: bytes,
    caption: str = "",
    date_str: str = "",
    width: int = DEVICE_WIDTH,
    height: int = DEVICE_HEIGHT,
    dither: bool = True,
) -> PreparedImage:
    """照片 + 底部文案渲染：底部圆角衬底块 + 日期小字 + 文案白字。

    文字在 6 色量化之前绘制（先画再量化，保证最终屏幕效果）。
    无中文字体或文案为空时，行为退化为纯照片。

    横屏照片：先按“用户横放相框”的视角渲染（画布宽高互换、照片 cover 铺满、
    文字画在底部），再整幅逆时针旋转 90° 回到 FPS6 竖屏——文字与照片同步旋转，
    相框顺时针横放观看时照片与文字均正立。
    """
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    landscape = img.size[0] > img.size[1]
    with_caption = bool(find_cjk_font() and caption.strip())

    if landscape:
        # 用户横放相框后的可视画布：宽高互换
        view_w, view_h = height, width
        canvas = _cover(img, view_w, view_h)
        if with_caption:
            _draw_caption(canvas, view_w, view_h, caption, date_str)
        canvas = canvas.rotate(ROTATE_LANDSCAPE, expand=True)  # (width, height)
    else:
        canvas = _cover(img, width, height)
        if with_caption:
            _draw_caption(canvas, width, height, caption, date_str)

    raw = _quantize_to_layout(canvas, width, height, dither)
    return PreparedImage(width=width, height=height, data=_pack_fps6(raw, width, height),
                         palette=SPECTRA6_PALETTE)


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
            x = 2 * k  # LTR：字节 k 对应像素对 (2k, 2k+1)
            px[x, y] = even_rgb
            px[x + 1, y] = odd_rgb
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def is_landscape(image_bytes: bytes) -> bool:
    """源图（EXIF 摆正后）是否横屏。用于 Web 预览按横放视角摆正。"""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    return img.size[0] > img.size[1]


def preview_png_landscape(prepared: PreparedImage, landscape: bool) -> bytes:
    """预览并摆正方向：横屏内容按用户横放相框的视角返回（1600x1200），竖屏原样。"""
    if not landscape:
        return preview_png(prepared)
    img = Image.open(io.BytesIO(preview_png(prepared))).rotate(-90, expand=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
