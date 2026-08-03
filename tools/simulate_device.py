#!/usr/bin/env python3
"""
simulate_device.py — 设备端渲染逻辑模拟（无真机验证）

按固件 gdeb0709e01_display_image() 的两阶段读取逻辑（先 IC0 全部行、
再 IC1 全部行），把 FPS6 数据区重组为设备 RAM 布局，再还原成屏幕
像素图。用于离线确认：
  - 服务端 FPS6 格式与固件读取逻辑匹配
  - 生成"设备视角" PNG 供人工检查

用法：
    python tools/simulate_device.py image.fps6 [-o device_view.png]
"""
import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from PIL import Image  # noqa: E402
from app.epd_image import SPECTRA6_PALETTE, NIBBLES  # noqa: E402

HEADER = 20
W, H = 1200, 1600
ROW_BYTES = W // 2          # 600
IC_ROW = ROW_BYTES // 2     # 300


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="FPS6 文件路径")
    ap.add_argument("-o", "--output", default="device_view.png")
    args = ap.parse_args()

    data = Path(args.input).read_bytes()
    assert data[:4] == b"FPS6", "not a FPS6 file"
    w, h, fmt = struct.unpack_from("<IIB", data, 4)
    assert (w, h) == (W, H) and fmt == 1, f"unsupported {w}x{h} fmt={fmt}"

    payload = data[HEADER:]

    # 固件逻辑：IC0 阶段读每行前 300B；IC1 阶段从 offset 300 起连续读 300B/行
    ic0 = bytearray()
    for y in range(H):
        ic0 += payload[y * ROW_BYTES:(y * ROW_BYTES) + IC_ROW]
    ic1 = bytearray()
    for y in range(H):
        ic1 += payload[y * ROW_BYTES + IC_ROW:(y + 1) * ROW_BYTES]

    # 设备 RAM 布局 -> 屏幕像素
    img = Image.new("RGB", (W, H))
    px = img.load()

    def fill_ic(ic_bytes: bytes, x_start: int):
        # 每行 IC_ROW 字节；字节 k 对应当前 IC 内的像素对（从右到左）
        for y in range(H):
            base = y * IC_ROW
            for k in range(IC_ROW):
                b = ic_bytes[base + k]
                hi = NIBBLES.index(b >> 4)
                lo = NIBBLES.index(b & 0xF)
                # 当前 IC 内最右像素对 = 字节 0
                local_x = IC_ROW * 2 - 2 - 2 * k
                px[x_start + local_x, y] = SPECTRA6_PALETTE[hi]
                px[x_start + local_x + 1, y] = SPECTRA6_PALETTE[lo]

    fill_ic(bytes(ic0), 600)   # IC0 -> 屏幕右半
    fill_ic(bytes(ic1), 0)     # IC1 -> 屏幕左半

    img.save(args.output)
    print(f"device view saved: {args.output} ({W}x{H})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
