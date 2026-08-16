"""T8 #9 — 帧格式文档权威归一。

docs/protocol/fps6-format.md 是 FPS6 帧格式的唯一人写权威：
palette_version 标注为恒 1 的保留字段；图像模块 docstring 不再维护
格式偏移表副本，指向协议文档；文档描述与 parse_fps6 校验行为一致。
"""
import re
import struct
from pathlib import Path

import pytest

from app import epd_image

DOC = Path(__file__).resolve().parents[2] / "docs" / "protocol" / "fps6-format.md"


def test_doc_declares_palette_version_reserved_and_sole_authority():
    text = DOC.read_text("utf-8")
    # 唯一人写权威声明
    assert "唯一人写权威" in text, "文档须声明自身为帧格式唯一人写权威"
    # palette_version 行须标注「恒为 1，保留字段」
    pv_lines = [l for l in text.splitlines() if "palette_version" in l]
    assert pv_lines, "文档须描述 palette_version 字段"
    assert any(("恒为 1" in l and "保留" in l) for l in pv_lines), (
        "palette_version 须标注为恒为 1 的保留字段"
    )


def test_epd_image_docstring_defers_to_protocol_doc():
    doc = epd_image.__doc__ or ""
    # 指向协议文档
    assert "docs/protocol/fps6-format.md" in doc, "docstring 须指向协议文档"
    # 偏移表副本特征不得残留（字段布局表、固定数据区字节数）
    assert "u32LE" not in doc, "docstring 不得维护帧头字段布局副本"
    assert "960000" not in doc, "docstring 不得维护数据区大小副本"


def test_doc_frame_fields_match_parse_fps6():
    """文档描述的帧字段与长度公式，与 parse_fps6 实际校验行为一致。

    从文档提取 magic/宽高/头长/数据区大小，按文档构造帧，
    验证 parse_fps6 接受合法帧、拒绝坏帧且失败消息可区分。
    """
    text = DOC.read_text("utf-8")
    magic = re.search(r'magic\s+"(\w{4})"', text).group(1)
    width = int(re.search(r"width\s*\(u32 LE\)\s*=\s*(\d+)", text).group(1))
    height = int(re.search(r"height\s*\(u32 LE\)\s*=\s*(\d+)", text).group(1))
    header_size, data_size = map(
        int, re.search(r"^(\d+)\s+(\d+)\s+数据区", text, re.M).groups()
    )

    # 文档字段与打包常量一致，数据区大小与长度公式自洽
    assert magic.encode() == epd_image.MAGIC
    assert (width, height) == (epd_image.DEVICE_WIDTH, epd_image.DEVICE_HEIGHT)
    assert header_size == epd_image.HEADER_SIZE
    assert data_size == width * height // 2
    # 文档写明校验口径：长度公式 + magic/长度必检、尺寸可选
    assert "20 + width*height/2" in text
    assert re.search(r"magic[^\n]*必检|必检[^\n]*magic", text)
    assert re.search(r"长度[^\n]*必检|必检[^\n]*长度", text)
    assert "可选" in text

    def frame(w=width, h=height, data_len=None, magic_=magic.encode()):
        data = bytes(data_len if data_len is not None else w * h // 2)
        return struct.pack(
            "<4sIIBB6x", magic_, w, h,
            epd_image.FORMAT_4BIT_DUAL_IC, epd_image.PALETTE_VERSION,
        ) + data

    # 按文档构造的合法帧被接受（含尺寸校验），palette_version 保留字段不参与校验
    ok = epd_image.parse_fps6(frame(), expected_size=(width, height))
    assert (ok.width, ok.height) == (width, height)
    with pytest.raises(ValueError, match="magic"):
        epd_image.parse_fps6(frame(magic_=b"XXXX"))
    with pytest.raises(ValueError, match="truncated"):
        epd_image.parse_fps6(frame(data_len=width * height // 2 - 1))
    with pytest.raises(ValueError, match="length mismatch"):
        epd_image.parse_fps6(frame() + b"\x00")
    with pytest.raises(ValueError, match="expected"):
        epd_image.parse_fps6(frame(w=width - 1), expected_size=(width, height))
