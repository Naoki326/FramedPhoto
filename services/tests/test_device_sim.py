"""
集成测试：模拟固件两阶段读取逻辑，验证与服务端 preview 完全一致。

固件 gdeb0709e01_display_image() 的正确读取模式（逐行交织，LTR）：
  阶段1（IC0 左半屏）：行 y 从数据区 offset y*600 起，读前 300B
  阶段2（IC1 右半屏）：行 y 从数据区 offset y*600+300 起，读后 300B

⚠️ 注意：IC0/IC1 数据在文件里是「逐行交织」的，不是两个连续大块。
此前此处注释曾把阶段2写成「从 offset 300 起连续读 300B/行」，那是错误的
线性读取模型；照此实现的 render_fps6_file() 会导致第二 IC 从第 2 行起错位、
画面被竖向拉伸。本测试的 _device_view 始终按行重组，故能正确复现真机效果。
另有早期版本把行内像素按从右到左（RTL）打包，实测导致整图镜像，
现统一为 LTR（字节 k ↔ 像素对 (2k, 2k+1)），与真机验证一致。

本测试用同样逻辑重组字节流并逆映射为像素，断言与 preview_png 完全一致，
以此在无真机时验证固件渲染路径与服务端格式匹配。
"""
import io

from PIL import Image, ImageChops

from app.epd_image import (
    HEADER_SIZE,
    DEVICE_WIDTH as W,
    DEVICE_HEIGHT as H,
    SPECTRA6_PALETTE,
    NIBBLES,
    prepare_image,
    preview_png,
)

ROW_BYTES = W // 2
IC_ROW = ROW_BYTES // 2


def _device_view(payload: bytes, colors=SPECTRA6_PALETTE) -> Image.Image:
    """按固件逻辑重组 [IC0 区][IC1 区] 并逆映射为屏幕像素图（LTR）。

    colors: 颜色表，默认理想色板（等价于 v1）；传 profile.device 可模拟
    校准后的真机观感（与 preview_png 一致）。
    """
    ic0 = bytearray()
    ic1 = bytearray()
    for y in range(H):
        ic0 += payload[y * ROW_BYTES:y * ROW_BYTES + IC_ROW]
        ic1 += payload[y * ROW_BYTES + IC_ROW:(y + 1) * ROW_BYTES]

    img = Image.new("RGB", (W, H))
    px = img.load()
    for x_start, ic in ((0, bytes(ic0)), (IC_ROW * 2, bytes(ic1))):  # ic0=左半, ic1=右半
        for y in range(H):
            base = y * IC_ROW
            for k in range(IC_ROW):
                b = ic[base + k]
                local_x = 2 * k  # LTR：字节 k ↔ 像素对 (2k, 2k+1)
                px[x_start + local_x, y] = colors[NIBBLES.index(b >> 4)]
                px[x_start + local_x + 1, y] = colors[NIBBLES.index(b & 0xF)]
    return img


def _random_like_image() -> bytes:
    """生成带多种颜色的测试图（纯色 + 渐变 + 区域色块）。"""
    import random

    rng = random.Random(42)
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        for x in range(W):
            if x < 400:
                px[x, y] = (255, 34, 34) if y < 800 else (0, 0, 255)
            elif x >= 800:
                px[x, y] = (0, 150, 60) if y < 800 else (255, 150, 0)
            else:
                px[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_device_render_matches_preview():
    src = _random_like_image()
    p = prepare_image(src, dither=False)
    device = _device_view(p.raw_index, colors=p.profile.device)
    server = Image.open(io.BytesIO(preview_png(p))).convert("RGB")

    assert device.size == server.size == (W, H)
    assert ImageChops.difference(device, server).getbbox() is None, \
        "设备模拟渲染与服务端预览不一致，请检查固件读取逻辑或 FPS6 格式"


def test_device_render_spot_check():
    """区域色块抽查：逻辑 (0,0) 红 / (1199,0) 蓝 / 中缝白。"""
    img = Image.new("RGB", (W, H), (255, 255, 255))
    for y in range(200):
        for x in range(200):
            img.putpixel((x, y), (255, 0, 0))
        for x in range(1000, 1200):
            img.putpixel((x, y), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    p = prepare_image(buf.getvalue(), dither=False)
    dev = _device_view(p.raw_index)
    assert dev.getpixel((0, 0)) == (255, 34, 34), "IC0 左半最左应为红"
    assert dev.getpixel((1199, 0)) == (0, 70, 200), "IC1 右半最右应为蓝"
    assert dev.getpixel((600, 0)) == (255, 255, 255), "中缝应为白"
