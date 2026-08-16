"""epd_image 模块测试（FPS6 4bit 双 IC 设备格式）。"""
import io
import struct

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
    SPECTRA6_PROFILE_V1,
    parse_fps6,
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


def test_fit_strategy():
    """竖屏图 cover 铺满；横屏图旋转 90° 后铺满（无黑边，不再裁成竖条）。"""
    # 竖屏：800x1600 纯白 → 全屏白，无黑边
    p = prepare_image(_make_image(800, 1600, color=(255, 255, 255)), dither=False)
    assert (p.width, p.height) == (W, H)
    assert set(p.raw_index) == {0x11}

    # 横屏纯白：旋转 90° 后仍全屏白，无黑边
    p2 = prepare_image(_make_image(2000, 1000, color=(255, 255, 255)), dither=False)
    assert (p2.width, p2.height) == (W, H)
    assert set(p2.raw_index) == {0x11}

    # 横屏左黑右白：旋转 90° 后上白下黑（cover 铺满，顶部=原图右半，底部=原图左半）
    img = Image.new("RGB", (2000, 1000), (0, 0, 0))
    for x in range(1000, 2000):
        for y in range(1000):
            img.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p3 = prepare_image(buf.getvalue(), dither=False)
    rb = W // 2
    assert set(p3.raw_index[0:rb]) == {0x11}                 # 顶部白（原右半）
    assert set(p3.raw_index[(H - 1) * rb:H * rb]) == {0x00}  # 底部黑（原左半）


def test_caption_rotates_with_landscape():
    """横屏照片的底部文案跟随照片旋转：渐变带落在数据区右缘（相框顺时针横放时在底部）。"""
    from app.epd_image import prepare_image_with_caption

    # 横屏纯白 + 文案：照片旋转铺满，文字/渐变带应在右缘，左缘仍为纯白照片
    p = prepare_image_with_caption(_make_image(2000, 1000, color=(255, 255, 255)),
                                   caption="横屏测试", date_str="2026.08.04", dither=False)
    raw = p.raw_index
    rb = W // 2
    # 左缘 120 列（x 0..239）为纯白照片区
    assert all(b == 0x11 for b in raw[0:120])
    # 右缘渐变带区域（x 960..1199，字节 480..599）存在非纯白字节（渐变中段量化后为黑/绿）
    right_band = [raw[y * rb + k] for y in range(H) for k in range(480, 600)]
    assert any(b != 0x11 for b in right_band)


def test_exif_orientation_applied():
    """手机照片 EXIF 方向：orientation=6 的横屏存储应被摆正为竖屏显示，不再横躺。"""
    from PIL import Image as PILImage

    # 存储像素 300x200（横屏）：左半黑、右半白，EXIF 标记 orientation=6（顺时针旋转 90° 显示）
    img = PILImage.new("RGB", (300, 200), (0, 0, 0))
    for x in range(150, 300):
        for y in range(200):
            img.putpixel((x, y), (255, 255, 255))
    exif = PILImage.Exif()
    exif[0x0112] = 6  # Orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)

    p = prepare_image(buf.getvalue(), dither=False)
    assert (p.width, p.height) == (W, H)
    rb = W // 2
    # 摆正后为 200x300 竖屏，cover 铺满全屏：上半黑、下半白
    assert set(p.raw_index[0:rb]) == {0x00}
    assert set(p.raw_index[(H - 1) * rb:H * rb]) == {0x11}


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
    """行内布局：前 300B 属 IC0（左半），后 300B 属 IC1（右半），像素从左到右。"""
    # 左半黑右半白的图像
    img = Image.new("RGB", (W, H), (0, 0, 0))
    for x in range(W // 2, W):
        for y in range(0, H, 16):
            img.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p = prepare_image(buf.getvalue(), dither=False)
    raw = p.raw_index
    # 检查第 0 行：字节 0（最左像素）应来自左半屏黑色区；字节 599（最右）来自白色区
    # 每行 600B，左半屏黑色区落在 k<300 区间（行内 k 0..299 对应 x 0..599）
    row0 = raw[0:600]
    assert row0[0] == 0x00  # 最左像素对 = 黑
    assert row0[599] == 0x11  # 最右像素对 = 白


def test_preview_roundtrip():
    p = prepare_image(_make_image(640, 480))
    png = preview_png(p)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    assert img.size == (W, H)


# ---------- parse_fps6：FPS6 帧解析与校验单一入口 ----------


def test_parse_roundtrip_identity():
    """prepare → parse_fps6 往返恒等：数据、宽高、profile（默认理想色板 v1）。"""
    p = prepare_image(_make_image(320, 240), profile="v1")
    parsed = parse_fps6(p.data)
    assert parsed.data == p.data
    assert (parsed.width, parsed.height) == (p.width, p.height)
    assert parsed.profile == p.profile
    # 帧不携带 profile 信息：v2 打包的帧解析结果仍为理想色板 v1
    v2 = prepare_image(_make_image(320, 240), profile="v2")
    assert parse_fps6(v2.data).profile is SPECTRA6_PROFILE_V1


def test_parse_rejects_bad_magic():
    """非 FPS6 帧拒绝，消息含 magic 关键词。"""
    png = _make_image(320, 240)
    with pytest.raises(ValueError, match="magic"):
        parse_fps6(png)
    with pytest.raises(ValueError, match="magic"):
        parse_fps6(b"FPS7" + b"\x00" * 960016)


def test_parse_rejects_truncated():
    """截断帧拒绝（header 不完整 / 数据区不完整），消息含 truncated 关键词。"""
    frame = prepare_image(_make_image(320, 240)).data
    with pytest.raises(ValueError, match="truncated"):
        parse_fps6(frame[:10])                     # header 都不完整
    with pytest.raises(ValueError, match="truncated"):
        parse_fps6(frame[:HEADER_SIZE + 1000])     # 数据区被截断


def test_parse_rejects_length_mismatch():
    """帧长于长度公式（尾部多余字节）拒绝，消息含 length、不含 truncated。"""
    frame = prepare_image(_make_image(320, 240)).data
    with pytest.raises(ValueError, match="length"):
        parse_fps6(frame + b"\x00" * 16)


def test_parse_size_check_optional():
    """尺寸校验可选：传入 expected_size 且不符 → ValueError；不传则只检 magic 与长度。"""
    # 自洽的 8x4 小帧：header 声明与数据区长度一致，但非设备尺寸
    small = struct.pack("<4sIIBB6x", MAGIC, 8, 4, FORMAT_4BIT_DUAL_IC, 1) \
        + b"\x11" * (8 * 4 // 2)
    parsed = parse_fps6(small)                    # 不传：尺寸不被检查
    assert (parsed.width, parsed.height) == (8, 4)
    assert parsed.data == small
    with pytest.raises(ValueError, match="size"):
        parse_fps6(small, expected_size=(DEVICE_WIDTH, DEVICE_HEIGHT))
    # 传入且相符：正常解析
    frame = prepare_image(_make_image(320, 240)).data
    full = parse_fps6(frame, expected_size=(DEVICE_WIDTH, DEVICE_HEIGHT))
    assert full.data == frame


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
        k = x // 2  # LTR：字节 k ↔ 像素对 (2k, 2k+1)
        byte = raw[y * (W // 2) + k]
        assert byte == (NIBBLES[i] << 4) | NIBBLES[i], f"color {i}: got {byte:02x}"
