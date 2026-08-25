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


# ---- 色域外高饱和色的墨水对混色（彩虹图白色块修复，ADR-0004）----
# 校准后的墨水普遍比理想色板暗（唯一亮彩墨是黄），亮青/亮品红这类色域外
# 高饱和色在「最近墨水」判定下（RGB/OKLab 皆然）必然命中唯一的亮墨——白，
# 整块退化为白色（实测彩虹图青/品红块白墨 83~96%）。修复：最近墨水为
# 黑/白且像素饱和度超过 CHROMA_SAT_GATE 时，改用「最近墨水对」最小二乘
# 混合解（纯青 → 白59%+蓝41%，天空 → 白79%+蓝21%），FS 误差扩散自动
# 实现该比例；浓彩偏置 chroma_bias>0 时把实现比例进一步推向彩色端点。

CHROMA_SAT_GATE = 0.35   # 触发墨水对混色的饱和度门槛（(max-min)/max）
MAX_CHROMA_BIAS = 4.0    # 浓彩偏置上限（管理台滑杆范围）
PURE_SNAP2 = 900         # 源色纯墨吸附：源色距最近墨水 ≤30 个 RGB 单位时
                         # 直接纯色输出（六纯色参考块/照片纯色区不参混色）


def _saturation(r: float, g: float, b: float) -> float:
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx == 0 else (mx - mn) / mx


_HULL_CACHE: dict[tuple, list[tuple[tuple[float, float, float], float]]] = {}


def _hull_facets(targets: tuple[tuple[int, int, int], ...]):
    """墨水凸包的面方程（一次预计算，按 targets 缓存）。

    6 个顶点最多 8 个面；每个面存外法线 n 与偏移 d（面上 n·p = d，
    包内 n·p <= d）。色域内颜色交给原有 6 墨涌现式混色（保真更好），
    只有色域外颜色才需要墨水对干预。
    """
    key = tuple(targets)
    cached = _HULL_CACHE.get(key)
    if cached is not None:
        return cached
    facets = []
    for a in range(6):
        for b in range(a + 1, 6):
            for c in range(b + 1, 6):
                pa, pb, pc = targets[a], targets[b], targets[c]
                ux, uy, uz = pb[0]-pa[0], pb[1]-pa[1], pb[2]-pa[2]
                vx, vy, vz = pc[0]-pa[0], pc[1]-pa[1], pc[2]-pa[2]
                nx, ny, nz = uy*vz-uz*vy, uz*vx-ux*vz, ux*vy-uy*vx
                if nx*nx + ny*ny + nz*nz < 1e-9:
                    continue
                d = nx*pa[0] + ny*pa[1] + nz*pa[2]
                rest = [targets[k] for k in range(6) if k not in (a, b, c)]
                vals = [nx*p[0] + ny*p[1] + nz*p[2] for p in rest]
                if all(v <= d + 1e-6 for v in vals):
                    facets.append(((nx, ny, nz), d))
                elif all(v >= d - 1e-6 for v in vals):
                    facets.append(((-nx, -ny, -nz), -d))
    _HULL_CACHE[key] = facets
    return facets


def _outside_hull(rgb: tuple[float, float, float],
                  targets: tuple[tuple[int, int, int], ...]) -> bool:
    """颜色是否在墨水凸包之外（任一面被违反即在外）。"""
    for (nx, ny, nz), d in _hull_facets(targets):
        if nx*rgb[0] + ny*rgb[1] + nz*rgb[2] > d + 1e-6:
            return True
    return False


def _best_pair(rgb: tuple[float, float, float],
               targets: tuple[tuple[int, int, int], ...],
               require_chromatic: bool = False) -> tuple[int, int, float]:
    """最小二乘墨水对混合解：返回 (i, j, lam)，mix = j + lam*(i - j)。

    在全部 21 个墨水对（含 i==j 单色特例）中选残差最小者；同残差时取
    彩色端点更近者（避免 λ 截断平局时任意命中黑白/黄色）。单色是
    墨水对的 λ=0/1 特例，残差不会劣于最近单墨水；混合则把色域外颜色
    显式分解为两种墨水的抖动比例，而不是交给最近墨水的隐性误差反馈。
    require_chromatic=True（饱和色干预用）：候选对必须含彩色墨水——
    否则色域外漂移色的最优解会退化成黑白对，色相全失。
    """
    best = None   # (res2, cdist, i, j, lam)
    r, g, b = rgb
    for i in range(6):
        pi = targets[i]
        for j in range(i, 6):
            if require_chromatic and i <= 1 and j <= 1:
                continue
            pj = targets[j]
            dx, dy, dz = pi[0] - pj[0], pi[1] - pj[1], pi[2] - pj[2]
            dd = dx * dx + dy * dy + dz * dz
            if dd == 0:
                lam = 1.0
            else:
                lam = ((r - pj[0]) * dx + (g - pj[1]) * dy + (b - pj[2]) * dz) / dd
                lam = max(0.0, min(1.0, lam))
            ex = pj[0] + lam * dx - r
            ey = pj[1] + lam * dy - g
            ez = pj[2] + lam * dz - b
            res2 = ex * ex + ey * ey + ez * ez
            # 彩色端点距离（平局决胜；两端均彩色取近者）
            ci = None if i <= 1 else (pi[0]-r)**2 + (pi[1]-g)**2 + (pi[2]-b)**2
            cj = None if j <= 1 else (pj[0]-r)**2 + (pj[1]-g)**2 + (pj[2]-b)**2
            cdist = min(c for c in (ci, cj) if c is not None) if (ci is not None or cj is not None) else 0.0
            key = (res2, cdist)
            if best is None or key < (best[0], best[1]):
                best = (res2, cdist, i, j, lam)
    return best[2], best[3], best[4]


_PLAN_CACHE: dict[tuple, tuple | None] = {}


def _intervention_plan(r0: float, g0: float, b0: float,
                       targets: tuple[tuple[int, int, int], ...]):
    """源色 -> 干预方案（按 8 级/通道分桶缓存，桶内用桶中心色计算）。

    方案：None（色域内，不干预）或
    (a, c, mixed, lam_c, ddx, ddy, ddz, dd)：a/c 为黑白端/彩色端索引，
    mixed 表示恰一端黑白（偏置参与），lam_c 彩色份额，(dd*) 墨水对轴向量。
    凸包判定 + 21 个墨水对最小二乘每桶只算一次，逐像素只做投影判定，
    避免逐像素重算把全图渲染拖慢近一个量级。
    """
    key = (targets, int(r0) >> 3, int(g0) >> 3, int(b0) >> 3)
    if key in _PLAN_CACHE:
        return _PLAN_CACHE[key]
    center = (((int(r0) >> 3) << 3) + 4,
              ((int(g0) >> 3) << 3) + 4,
              ((int(b0) >> 3) << 3) + 4)
    if not _outside_hull(center, targets):
        plan = None
    else:
        i, j, lam = _best_pair(center, targets, require_chromatic=True)
        if i == j:
            plan = (i, i, False, 1.0, 0.0, 0.0, 0.0, 0.0)
        else:
            a, c = (i, j) if i <= 1 else (j, i)
            lam_c = (1.0 - lam) if a == i else lam
            mixed = (i <= 1) != (j <= 1)
            va, vc = targets[a], targets[c]
            ddx, ddy, ddz = vc[0] - va[0], vc[1] - va[1], vc[2] - va[2]
            dd = ddx * ddx + ddy * ddy + ddz * ddz
            plan = (a, c, mixed, lam_c, ddx, ddy, ddz, dd)
    _PLAN_CACHE[key] = plan
    return plan


def _pick_ink(r0: float, g0: float, b0: float,
              r2: float, g2: float, b2: float,
              targets: tuple[tuple[int, int, int], ...],
              chroma_bias: float = 0.0) -> tuple[int, float, float, float]:
    """量化取墨，返回 (墨水索引, 有效量化基准色)。

    低饱和或色域内：最近墨水（基准 = 调整后像素）。
    色域外高饱和色：墨水对混合（纯墨吸附除外）——
      - 墨水对方案由源色分桶缓存（_intervention_plan）；
      - 浓彩偏置把目标彩色份额 λ_c 放大为 λ_c·(1+bias·sat)；
      - 量化基准改为轴上虚拟源 v0 = a + λ_c_eff·(c-a)，误差只在轴上
        传播——1D FS 占空比精确实现 λ_c_eff，纹理稳定有序（自由 6 墨 FS
        在色域外饱和色上会漂移失稳，抖出成团杂色）。
    """
    idx = _nearest_index((r2, g2, b2), targets, "rgb")
    sat = _saturation(r0, g0, b0)
    if sat <= CHROMA_SAT_GATE:
        return idx, r2, g2, b2
    if not _outside_hull((r0, g0, b0), targets):
        # 色域内的亮饱和色：6 墨涌现式混色（FS 误差反馈）平均保真更好，不干预
        return idx, r2, g2, b2
    # 纯墨吸附：源色距某墨水 ≤30 单位时直接纯色输出（校准参考块/照片纯色区）
    si = _nearest_index((r0, g0, b0), targets, "rgb")
    pr, pg, pb = targets[si]
    if (pr - r0) ** 2 + (pg - g0) ** 2 + (pb - b0) ** 2 <= PURE_SNAP2:
        return si, r2, g2, b2
    # 色域外高饱和色：墨水对混色（自由 6 墨 FS 在这类颜色上漂移失稳，会抖出
    # 成团的杂色斑块——实测彩虹图绿区红块/竖条纹；1D 沿轴占空比则是稳定
    # 的有序细抖动）
    plan = _intervention_plan(r0, g0, b0, targets)
    if plan is None:
        # 凸包边界附近的桶中心可能落回色域内——退回最近墨水
        return idx, r2, g2, b2
    a, c, mixed, lam_c, ddx, ddy, ddz, dd = plan
    if dd == 0.0:
        return a, r2, g2, b2
    lam_c_eff = min(1.0, lam_c * (1.0 + chroma_bias * sat)) if mixed else lam_c
    va = targets[a]
    # 轴上虚拟源（含漂移）：占空比目标 λ_c_eff，误差沿轴反馈。
    # 漂移取自已限幅的 r2（FS 残差有界，防全图纹理失稳）；虚拟基准本身
    # 在色域内，再 clamp 一次保证残差有界。
    def _cl(v: float) -> float:
        return max(0.0, min(255.0, v))
    vr = _cl(va[0] + lam_c_eff * ddx + (r2 - r0))
    vg = _cl(va[1] + lam_c_eff * ddy + (g2 - g0))
    vb = _cl(va[2] + lam_c_eff * ddz + (b2 - b0))
    proj = ((vr - va[0]) * ddx + (vg - va[1]) * ddy + (vb - va[2]) * ddz) / dd
    pick = c if proj > 0.5 else a
    return pick, vr, vg, vb


def _effective_chroma_bias(value: float | None) -> float:
    """解析浓彩偏置：显式传值优先（测试/工具隔离），否则读运行时配置。"""
    if value is not None:
        v = float(value)
    else:
        try:
            from app.runtime_config import get
            v = float(get("chroma_bias", 0.0) or 0.0)
        except Exception:
            v = 0.0
    return max(0.0, min(MAX_CHROMA_BIAS, v))


def _floyd_steinberg(pixels: list[list[tuple[int, int, int]]],
                     profile: Spectra6Profile,
                     chroma_bias: float = 0.0) -> list[list[int]]:
    """6 色板 Floyd–Steinberg 抖动，返回每像素色板索引 0..5。

    distance="rgb"：误差在 RGB 传播，取墨走 _pick_ink（色域外高饱和色
    的墨水对混色修复在此生效，见 CHROMA_SAT_GATE 注释）。
    distance="oklab"：误差传播与颜色选择统一在 OKLab 空间（实验分支，
    无墨水对混色修复）。
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

    # rgb：误差在 RGB 传播；取墨与残差都基于限幅后的值（[0,255]），
    # 不限幅会让误差残差无界累积、大面积平色区出现条纹/杂色失稳
    # （实测淡带绿区混入红墨、亮带竖向条纹）；高饱和色域外色走墨水对
    # 混色（_pick_ink 返回墨水索引 + 有效量化基准色，误差按基准色传播）
    for y in range(h):
        for x in range(w):
            r, g, b = pixels[y][x]
            er, eg, eb = err[y][x]
            r2 = max(0, min(255, r + er))
            g2 = max(0, min(255, g + eg))
            b2 = max(0, min(255, b + eb))
            idx, qr_base, qg_base, qb_base = _pick_ink(r, g, b, r2, g2, b2,
                                                        targets, chroma_bias)
            out[y][x] = idx
            pr, pg, pb = targets[idx]
            qr, qg, qb = (qr_base - pr), (qg_base - pg), (qb_base - pb)
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
                        profile: Spectra6Profile,
                        chroma_bias: float = 0.0) -> bytes:
    """RGB 图像（已适配目标尺寸）-> FPS6 数据区。"""
    targets, distance = profile.targets, profile.distance
    pix = list(img.getdata())
    if dither:
        rows = [pix[y * width:(y + 1) * width] for y in range(height)]
        indices = _floyd_steinberg(rows, profile, chroma_bias)
    elif distance == "rgb":
        # 非抖动路径同样应用墨水对混色（与抖动行为一致；源像素即调整后像素，
        # 只取墨水索引）
        indices = [[_pick_ink(p[0], p[1], p[2], p[0], p[1], p[2], targets, chroma_bias)[0]
                    for p in pix[y * width:(y + 1) * width]] for y in range(height)]
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
    chroma_bias: float | None = None,
) -> PreparedImage:
    """输入任意格式图片字节，输出 FPS6 设备格式。

    profile: "v1"（历史 RGB 距离 + 理想色观感）或 "v2"（校准色空间）。
    chroma_bias: 浓彩偏置 0..4（None = 读运行时配置 chroma_bias，
    默认 0）；色域外高饱和色的墨水对混合向彩色端偏移的强度。
    """
    if (width, height) != (DEVICE_WIDTH, DEVICE_HEIGHT):
        raise ValueError(f"only {DEVICE_WIDTH}x{DEVICE_HEIGHT} supported, got {width}x{height}")

    prof = get_profile(profile) if isinstance(profile, str) else profile
    bias = _effective_chroma_bias(chroma_bias)

    img = Image.open(io.BytesIO(image_bytes))
    img = _fit(img, width, height)

    # 量化+抖动集中在 _quantize_to_layout 内完成（之前这里冗余算过一遍，从未使用）
    raw = _quantize_to_layout(img, width, height, dither, prof, bias)
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
    chroma_bias: float | None = None,
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
    bias = _effective_chroma_bias(chroma_bias)
    raw = _quantize_to_layout(canvas, width, height, dither, prof, bias)
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


def make_thumbnail(image_bytes: bytes, max_side: int = 500, quality: int = 80) -> bytes:
    """原始图片 -> JPEG 缩略图字节（量化前原图缩放，省流量，ADR-0001）。

    供各预览端点与置顶显示的原图缩略图落盘共用。
    """
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
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
