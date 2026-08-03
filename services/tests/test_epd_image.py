"""epd_image 模块测试（FPS6 4bit 双 IC 设备格式）。"""
import io

import pytest
from PIL import Image

from app.epd_image import (
    HEADER_SIZE,
    MAGIC,
    FORMAT_4BIT_DUAL_IC,
    DEVICE_WIDTH,
    DEVICE_HEIGHT,
    NIBBLES,
    SPECTRA6_PALETTE,
    prepare_image,
    preview_png,
)

W, H = DEVICE_WIDTH, DEVICE_HEIGHT


def _make_image(w: int, h: int, color=(128, 128, 128), mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_header():
    p = prepare_image(_make_image(400, 600))
    assert p.data[:4] == MAGIC
    assert p.width == W and p.height == H
    assert p.data[12] == FORMAT_4BIT_DUAL_IC
    assert len(p.data) == HEADER_SIZE + W * H // 2  # 20 + 960000


def test_fit_cover():
    # 超宽图应裁剪为竖屏目标尺寸
    p = prepare_image(_make_image(3000, 1000))
    assert (p.width, p.height) == (W, H)


def test_pure_white_layout():
    """纯白图：所有像素白(0x1)，数据区每个字节应为 0x11。"""
    p = prepare_image(_make_image(W, H, color=(255, 255, 255)), dither=False)
    assert len(set(p.raw_index)) == 1
    assert p.raw_index[0] == 0x11
    # 数据区长度与随机位置抽查
    assert p.raw_index[H * W // 2 // 2] == 0x11
    assert p.raw_index[-1] == 0x11


def test_pure_black_layout():
    p = prepare_image(_make_image(W, H, color=(0, 0, 0)), dither=False)
    assert set(p.raw_index) == {0x00}


def test_dual_ic_row_split():
    """行内布局：前 300B 属 IC0（右半），后 300B 属 IC1（左半）。"""
    # 左半黑右半白的图像
    img = Image.new("RGB", (W, H), (0, 0, 0))
    for x in range(W // 2, W):
        for y in range(0, H, 16):
            img.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p = prepare_image(buf.getvalue(), dither=False)
    raw = p.raw_index
    # 检查第 0 行：字节 0（最右像素）应来自右半屏白色区；字节 599（最左）来自黑色区
    # 每行 600B，右半屏白色区域落在 k<300 区间（行内 k 0..299 对应 x 1198..600）
    row0 = raw[0:600]
    assert row0[0] == 0x11  # 最右像素对 = 白
    assert row0[599] == 0x00  # 最左像素对 = 黑


def test_preview_roundtrip():
    p = prepare_image(_make_image(640, 480))
    png = preview_png(p)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    assert img.size == (W, H)


def test_grayscale_input():
    p = prepare_image(_make_image(100, 100, color=128, mode="L"))
    assert len(p.raw_index) == W * H // 2


def test_nibble_encoding_values():
    """颜色索引到设备 nibble 的映射与官方一致。"""
    assert NIBBLES == (0x0, 0x1, 0x2, 0x3, 0x5, 0x6)


def test_invalid_image_raises():
    with pytest.raises(Exception):
        prepare_image(b"not an image")


def test_palette_color_count():
    assert len(SPECTRA6_PALETTE) == 6


def test_color_nibble_mapping():
    """6 色到设备 nibble 的映射：红 -> 0x3, 蓝 -> 0x5（顺序与 palette 一致）。"""
    from app.epd_image import NIBBLES, prepare_image

    # 构造 6 色图（各色一块），验证每色编码正确
    colors = [(0, 0, 0), (255, 255, 255), (255, 150, 0), (255, 34, 34), (0, 70, 200), (0, 150, 60)]
    img = Image.new("RGB", (W, H), (255, 255, 255))
    band = H // 6
    for i, c in enumerate(colors):
        for y in range(i * band, (i + 1) * band):
            for x in range(0, 200):
                img.putpixel((x, y), c)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p = prepare_image(buf.getvalue(), dither=False)
    raw = p.raw_index

    for i, _ in enumerate(colors):
        y = i * band + 10
        x = 10  # 逻辑坐标
        k = (W - 2 - x) // 2
        byte = raw[y * (W // 2) + k]
        assert byte == (NIBBLES[i] << 4) | NIBBLES[i], f"color {i}: got {byte:02x}"
