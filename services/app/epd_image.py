"""
epd_image.py — 为 E Ink Spectra 6 屏幕准备图像。

流程：
  1. 解码输入图片（Pillow），按 EXIF 方向摆正（手机照片）
  2. 适配到 1200x1600：竖屏/方形图 cover（居中裁剪铺满）；横屏图旋转 90° 后
     cover 铺满（相框顺时针横放观看，照片正立、无黑边，文字随照片一起旋转）
  3. 量化到 Spectra 6 色板（6 色），Floyd–Steinberg 抖动补偿色阶损失
  4. 封装为 FPS6 设备格式（4bit/像素，双 IC 布局），设备端直接转发到驱动

FPS6 帧格式（帧头字段布局、双 IC 行布局、颜色 nibble、LTR 打包历史说明）
以 docs/protocol/fps6-format.md 为唯一人写权威，本模块不重复维护格式说明；
固件侧常量见 firmware/main/fps6_format.h，驱动对接见
firmware/components/gdey_epd/（gdeb0709e01_display_image 直接接受数据区）。
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

# 校准 profile 文件：tools/calibrate_profile.py 拍照采样后写入，
# 存在时自动覆盖 SPECTRA6_PROFILE_V2 的设备观感色（device）。
# 可用环境变量 FRAMEDPHOTO_CALIBRATED_PROFILE 覆盖路径（测试隔离用）。
import os as _os
_calibrated_path_env = _os.environ.get("FRAMEDPHOTO_CALIBRATED_PROFILE", "")
CALIBRATED_PROFILE_PATH = (
    Path(_calibrated_path_env) if _calibrated_path_env
    else Path(__file__).resolve().parent / "profiles" / "calibrated.json"
)

DEVICE_WIDTH = 1200
DEVICE_HEIGHT = 1600

# 横屏照片旋转角度（Pillow 正角 = 逆时针）：旋转后 cover 铺满竖屏，
# 用户把相框顺时针横放（顶部向右）即可正立观看，文字随照片一起旋转。
ROTATE_LANDSCAPE = 90

# 设备颜色 nibble（官方 GDEP133C02.h：BLACK 0x0 WHITE 0x1 YELLOW 0x2 RED 0x3 BLUE 0x5 GREEN 0x6）
NIBBLES = (0x0, 0x1, 0x2, 0x3, 0x5, 0x6)
NIBBLE_TO_INDEX = {n: i for i, n in enumerate(NIBBLES)}

# Spectra 6 六色近似 sRGB —— 顺序与设备 nibble 一一对应（索引 i -> NIBBLES[i]）
# 保留为「理想色板」，用于 v1 行为兼容与 A/B 对比。
SPECTRA6_PALETTE: list[tuple[int, int, int]] = [
    (0, 0, 0),          # 0x0 黑
    (255, 255, 255),    # 0x1 白
    (255, 150, 0),      # 0x2 黄（设备黄偏橙）
    (255, 34, 34),      # 0x3 红
    (0, 70, 200),       # 0x5 蓝
    (0, 150, 60),       # 0x6 绿
]


@dataclass(frozen=True)
class Spectra6Profile:
    """调色板双色模型：

    targets: 量化目标色（输入色空间的代表色，决定像素归到哪个墨水）
    device:  该墨水在真机上的实际观感颜色（仅用于预览/模拟，不参与量化）
    distance: 颜色距离算法（"rgb" 欧氏 / "oklab" 感知）
    """
    name: str
    targets: tuple[tuple[int, int, int], ...]
    device: tuple[tuple[int, int, int], ...]
    distance: str = "oklab"

    def __post_init__(self) -> None:
        if len(self.targets) != len(self.device) != 6:
            raise ValueError("profile must contain exactly 6 colors")


# v1：历史行为。量化目标与设备观感都用理想 sRGB 色，RGB 欧氏距离。
SPECTRA6_PROFILE_V1 = Spectra6Profile(
    name="v1",
    targets=tuple(SPECTRA6_PALETTE),
    device=tuple(SPECTRA6_PALETTE),
    distance="rgb",
)

# v2：校准模型。校准的本质是建立「理想色板 ↔ 屏幕真实观感」的整体映射：
# 校准后的六色（calibrated.json 的 device 色）同时作为量化目标（targets）与
# 预览渲染色（device）——量化在屏幕显示色空间进行，源色在真实观感色里找
# 最近墨水（+抖动混色），因此黄色自动混合成棕黄、其他颜色统一按同一套逻辑处理，
# 预览与真机一致。未校准时退化为理想色（与 v1 量化一致）。
SPECTRA6_PROFILE_V2 = Spectra6Profile(
    name="v2",
    targets=(
        (0, 0, 0),          # 0x0 黑
        (255, 255, 255),    # 0x1 白
        (165, 175, 94),     # 0x2 黄（校准棕黄占位，待 calibrated.json 覆盖）
        (109, 21, 17),      # 0x3 红（实拍占位）
        (33, 65, 127),      # 0x5 蓝（实拍占位）
        (86, 120, 95),      # 0x6 绿（实拍占位）
    ),
    device=(
        (0, 0, 0),          # 0x0 黑
        (255, 255, 255),    # 0x1 白
        (165, 175, 94),     # 0x2 黄
        (109, 21, 17),      # 0x3 红
        (33, 65, 127),      # 0x5 蓝
        (86, 120, 95),      # 0x6 绿
    ),
    distance="rgb",
)

_PROFILES: dict[str, Spectra6Profile] = {
    p.name: p for p in (SPECTRA6_PROFILE_V1, SPECTRA6_PROFILE_V2)
}


def _load_calibrated_profile() -> Spectra6Profile | None:
    """读取 calibrate_profile.py 生成的校准 JSON，覆盖 v2 的 device 色。"""
    if not CALIBRATED_PROFILE_PATH.exists():
        return None
    import json
    try:
        data = json.loads(CALIBRATED_PROFILE_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    device = [tuple(c) for c in data.get("device", [])]
    if len(device) != 6 or not all(len(c) == 3 for c in device):
        return None
    return Spectra6Profile(
        name="v2",
        targets=tuple(device),    # 校准色即量化目标（屏幕显示色空间）
        device=tuple(device),
        distance="rgb",
    )


_calibrated = _load_calibrated_profile()
if _calibrated is not None:
    _PROFILES["v2"] = _calibrated
    SPECTRA6_PROFILE_V2 = _calibrated


def reload_calibrated_profile() -> Spectra6Profile:
    """重新读取 calibrated.json 并热更新 v2 profile（校准照片上传后调用，
    无需重启服务即生效）。清除后（文件不存在）恢复默认占位。"""
    global SPECTRA6_PROFILE_V2
    prof = _load_calibrated_profile()
    if prof is None:
        prof = Spectra6Profile(
            name="v2",
            targets=(
                (0, 0, 0),          # 0x0 黑
                (255, 255, 255),    # 0x1 白
                (165, 175, 94),     # 0x2 黄
                (109, 21, 17),      # 0x3 红
                (33, 65, 127),      # 0x5 蓝
                (86, 120, 95),      # 0x6 绿
            ),
            device=(
                (0, 0, 0),          # 0x0 黑
                (255, 255, 255),    # 0x1 白
                (165, 175, 94),     # 0x2 黄
                (109, 21, 17),      # 0x3 红
                (33, 65, 127),      # 0x5 蓝
                (86, 120, 95),      # 0x6 绿
            ),
            distance="rgb",
        )
    SPECTRA6_PROFILE_V2 = prof
    _PROFILES["v2"] = prof
    _BYTE_LOOKUP_CACHE.pop(prof.name, None)   # 清预览查表缓存
    return prof


def get_profile(name: str) -> Spectra6Profile:
    try:
        return _PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown profile {name!r}, available: {sorted(_PROFILES)}") from None


@dataclass
class PreparedImage:
    width: int
    height: int
    data: bytes            # FPS6 完整字节流（含头）
    profile: Spectra6Profile = SPECTRA6_PROFILE_V1

    @property
    def raw_index(self) -> bytes:
        """数据区：width*height/2 字节设备布局（帧格式见协议文档）。"""
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


# ---- 感知颜色（OKLab，Björn Ottosson）----
# 预计算缓存：输入颜色按 16 级/通道量化后查表（最多 4096 项），避免
# 每像素重复做 sRGB 线性化与立方根，1.2M 像素下代价可忽略。


def _rgb_to_oklab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (c / 255.0 for c in rgb)

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = lin(r), lin(g), lin(b)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = l ** (1 / 3), m ** (1 / 3), s ** (1 / 3)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


_OKLAB_CACHE: dict[int, tuple[float, float, float]] = {}


def _oklab_cached(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    key = ((int(rgb[0]) >> 4) << 8) | ((int(rgb[1]) >> 4) << 4) | (int(rgb[2]) >> 4)
    v = _OKLAB_CACHE.get(key)
    if v is None:
        v = _rgb_to_oklab((int(rgb[0]), int(rgb[1]), int(rgb[2])))
        _OKLAB_CACHE[key] = v
    return v


def _nearest_index(rgb: tuple[int, int, int],
                   targets: tuple[tuple[int, int, int], ...] = tuple(SPECTRA6_PALETTE),
                   distance: str = "rgb") -> int:
    """返回 rgb 距离最近的目标色索引。distance: "rgb" | "oklab"。"""
    best, best_d = 0, float("inf")
    if distance == "rgb":
        r, g, b = rgb
        for i, (pr, pg, pb) in enumerate(targets):
            d = (pr - r) ** 2 + (pg - g) ** 2 + (pb - b) ** 2
            if d < best_d:
                best, best_d = i, d
        return best
    if distance == "oklab":
        lab = _oklab_cached(rgb)
        labs = [None] * len(targets)
        for i, t in enumerate(targets):
            labs[i] = _oklab_cached(t)
        for i, tl in enumerate(labs):
            d = ((lab[0] - tl[0]) ** 2 + (lab[1] - tl[1]) ** 2 + (lab[2] - tl[2]) ** 2)
            if d < best_d:
                best, best_d = i, d
        return best
    raise ValueError(f"unknown distance {distance!r}")


def _floyd_steinberg(pixels: list[list[tuple[int, int, int]]],
                     profile: Spectra6Profile) -> list[list[int]]:
    """6 色板 Floyd–Steinberg 抖动，返回每像素色板索引 0..5。

    distance="rgb"：与 v1 完全一致（RGB 误差传播 + RGB 距离）。
    distance="oklab"：误差传播与颜色选择统一在 OKLab 空间，
    避免「RGB 误差 + OKLab 选择」两个空间不一致导致的误差补偿失效
    （曾造成渐变区抖动错乱、大面积噪声）。
    """
    targets = profile.targets
    distance = profile.distance
    h, w = len(pixels), len(pixels[0])
    err: list[list[tuple[float, float, float]]] = [[(0.0, 0.0, 0.0)] * w for _ in range(h)]
    out: list[list[int]] = [[0] * w for _ in range(h)]

    if distance == "oklab":
        # 预转换：目标色 OKLab + 逐像素缓存，误差在 OKLab 传播
        target_labs = [_oklab_cached(t) for t in targets]
        for y in range(h):
            for x in range(w):
                el, ea, eb = err[y][x]
                lab = _oklab_cached(pixels[y][x])
                l2 = lab[0] + el
                a2 = lab[1] + ea
                b2 = lab[2] + eb
                # OKLab 最近邻
                best, best_d = 0, float("inf")
                for i, tl in enumerate(target_labs):
                    d = ((l2 - tl[0]) ** 2 + (a2 - tl[1]) ** 2 + (b2 - tl[2]) ** 2)
                    if d < best_d:
                        best, best_d = i, d
                out[y][x] = best
                tl = target_labs[best]
                ql = l2 - tl[0]
                qa = a2 - tl[1]
                qb = b2 - tl[2]
                if x + 1 < w:
                    err[y][x + 1] = (err[y][x + 1][0] + ql * 7 / 16,
                                     err[y][x + 1][1] + qa * 7 / 16,
                                     err[y][x + 1][2] + qb * 7 / 16)
                if y + 1 < h:
                    err[y + 1][x] = (err[y + 1][x][0] + ql * 5 / 16,
                                     err[y + 1][x][1] + qa * 5 / 16,
                                     err[y + 1][x][2] + qb * 5 / 16)
                    if x > 0:
                        err[y + 1][x - 1] = (err[y + 1][x - 1][0] + ql * 3 / 16,
                                             err[y + 1][x - 1][1] + qa * 3 / 16,
                                             err[y + 1][x - 1][2] + qb * 3 / 16)
                    if x + 1 < w:
                        err[y + 1][x + 1] = (err[y + 1][x + 1][0] + ql * 1 / 16,
                                             err[y + 1][x + 1][1] + qa * 1 / 16,
                                             err[y + 1][x + 1][2] + qb * 1 / 16)
        return out

    # rgb：历史行为，误差在 RGB 传播
    for y in range(h):
        for x in range(w):
            r, g, b = pixels[y][x]
            er, eg, eb = err[y][x]
            r2 = max(0, min(255, r + er))
            g2 = max(0, min(255, g + eg))
            b2 = max(0, min(255, b + eb))
            idx = _nearest_index((r2, g2, b2), targets, "rgb")
            out[y][x] = idx
            pr, pg, pb = targets[idx]
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


def _quantize_to_layout(img: Image.Image, width: int, height: int, dither: bool,
                        profile: Spectra6Profile) -> bytes:
    """RGB 图像（已适配目标尺寸）-> FPS6 数据区。"""
    targets, distance = profile.targets, profile.distance
    pix = list(img.getdata())
    if dither:
        rows = [pix[y * width:(y + 1) * width] for y in range(height)]
        indices = _floyd_steinberg(rows, profile)
    else:
        indices = [[_nearest_index(pix[y * width + x], targets, distance)
                    for x in range(width)] for y in range(height)]
    return _to_device_layout(indices, width)


def _pack_fps6(raw: bytes, width: int, height: int) -> bytes:
    header = struct.pack(
        "<4sIIBB6x", MAGIC, width, height, FORMAT_4BIT_DUAL_IC, PALETTE_VERSION
    )
    return header + raw


def parse_fps6(frame: bytes,
               expected_size: tuple[int, int] | None = None) -> PreparedImage:
    """解析并校验 FPS6 帧，返回 PreparedImage（默认理想色板 v1 profile）。

    帧格式见 docs/protocol/fps6-format.md（唯一人写权威）。
    magic 与长度公式必检；expected_size 传入时校验宽高
    （上传场景传入，坏图自检等内部场景不传）。非法输入抛 ValueError，
    三种失败消息可区分：bad magic / truncated（短于长度公式）/
    length mismatch（长于长度公式）。
    帧字节不携带 profile 信息，解析结果恒带理想色板
    （SPECTRA6_PROFILE_V1；ADR-0001：观感色概念已废除）。
    """
    if len(frame) < HEADER_SIZE:
        raise ValueError(
            f"truncated FPS6 frame: {len(frame)} bytes < {HEADER_SIZE}-byte header"
        )
    if frame[:4] != MAGIC:
        raise ValueError(f"bad FPS6 magic: {frame[:4]!r}")
    width, height = struct.unpack_from("<II", frame, 4)
    expected_len = HEADER_SIZE + width * height // 2
    if len(frame) < expected_len:
        raise ValueError(
            f"truncated FPS6 frame: {len(frame)} bytes < {expected_len}"
            f" for {width}x{height}"
        )
    if len(frame) > expected_len:
        raise ValueError(
            f"FPS6 frame length mismatch: {len(frame)} bytes > {expected_len}"
            f" for {width}x{height}"
        )
    if expected_size is not None and (width, height) != tuple(expected_size):
        raise ValueError(
            f"FPS6 frame size {width}x{height} != expected"
            f" {expected_size[0]}x{expected_size[1]}"
        )
    return PreparedImage(width=width, height=height, data=frame,
                         profile=SPECTRA6_PROFILE_V1)


def prepare_image(
    image_bytes: bytes,
    width: int = DEVICE_WIDTH,
    height: int = DEVICE_HEIGHT,
    dither: bool = True,
    profile: str | Spectra6Profile = "v2",
) -> PreparedImage:
    """输入任意格式图片字节，输出 FPS6 设备格式。

    profile: "v1"（历史 RGB 距离 + 理想色观感）或 "v2"（OKLab 感知距离
    + 校准观感色）；默认 v2。
    """
    if (width, height) != (DEVICE_WIDTH, DEVICE_HEIGHT):
        raise ValueError(f"only {DEVICE_WIDTH}x{DEVICE_HEIGHT} supported, got {width}x{height}")

    prof = get_profile(profile) if isinstance(profile, str) else profile

    img = Image.open(io.BytesIO(image_bytes))
    img = _fit(img, width, height)

    # 量化+抖动集中在 _quantize_to_layout 内完成（之前这里冗余算过一遍，从未使用）
    raw = _quantize_to_layout(img, width, height, dither, prof)
    return PreparedImage(width=width, height=height, data=_pack_fps6(raw, width, height),
                         profile=prof)


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
    profile: str | Spectra6Profile = "v2",
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

    prof = get_profile(profile) if isinstance(profile, str) else profile
    raw = _quantize_to_layout(canvas, width, height, dither, prof)
    return PreparedImage(width=width, height=height, data=_pack_fps6(raw, width, height),
                         profile=prof)


# 字节值 -> (偶数像素 RGB, 奇数像素 RGB) 的 256 项查找表（预览用）
# 非法 nibble（如 0x4/0x7+）在合法数据中不应出现，兜底映射为黑
# 预览用 prepared.profile.device 渲染：模拟真机观感（校准后贴近实拍）。
_BYTE_LOOKUP_CACHE: dict[str, list[tuple[tuple[int, int, int], tuple[int, int, int]]]] = {}


def _byte_lookup(profile: Spectra6Profile) -> list[tuple[tuple[int, int, int], tuple[int, int, int]]]:
    lut = _BYTE_LOOKUP_CACHE.get(profile.name)
    if lut is None:
        colors = profile.device
        lut = [
            (
                colors[NIBBLE_TO_INDEX.get(b >> 4, 0)],
                colors[NIBBLE_TO_INDEX.get(b & 0xF, 0)],
            )
            for b in range(256)
        ]
        _BYTE_LOOKUP_CACHE[profile.name] = lut
    return lut


def preview_png(prepared: PreparedImage) -> bytes:
    """把 FPS6 数据区还原成 PNG 预览（Web/CLI 查看转换效果）。

    用 prepared.profile.device 颜色渲染：v2（已校准）时模拟真机观感，
    v1 时等价于历史行为（理想色）。
    """
    raw = prepared.raw_index
    row_bytes = prepared.width // 2
    img = Image.new("RGB", (prepared.width, prepared.height))
    px = img.load()
    lookup = _byte_lookup(prepared.profile)
    for y in range(prepared.height):
        base = y * row_bytes
        for k in range(row_bytes):
            even_rgb, odd_rgb = lookup[raw[base + k]]
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
