"""epd_image 模块测试。"""
import io

import pytest
from PIL import Image

from app.epd_image import (
    HEADER_SIZE,
    MAGIC,
    FORMAT_INDEX,
    SPECTRA6_PALETTE,
    prepare_image,
    preview_png,
)


def _make_image(w: int, h: int, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h)).save(buf, format="PNG")
    return buf.getvalue()


def test_header():
    p = prepare_image(_make_image(400, 600), width=1200, height=1600)
    assert p.data[:4] == MAGIC
    assert p.width == 1200 and p.height == 1600
    assert p.data[12] == FORMAT_INDEX
    assert len(p.data) == HEADER_SIZE + 1200 * 1600


def test_fit_cover():
    # 超宽图应裁剪为竖屏目标尺寸
    p = prepare_image(_make_image(3000, 1000), width=1200, height=1600)
    assert (p.width, p.height) == (1200, 1600)


def test_palette_indices_valid():
    p = prepare_image(_make_image(1200, 1600))
    assert all(i < len(SPECTRA6_PALETTE) for i in p.raw_index)


def test_preview_roundtrip():
    p = prepare_image(_make_image(640, 480), width=1200, height=1600)
    png = preview_png(p)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    assert img.size == (1200, 1600)


def test_grayscale_input():
    # 灰度图也要能转换（1 通道输入）
    p = prepare_image(_make_image(100, 100, mode="L"), width=200, height=200)
    assert len(p.raw_index) == 200 * 200


def test_invalid_image_raises():
    with pytest.raises(Exception):
        prepare_image(b"not an image", width=10, height=10)
