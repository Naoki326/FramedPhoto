"""
display.py — 置顶显示：一次性上传切换当前显示（不入照片库、不入库分析）。

管理台随时上传 / 覆盖 / 清除一张临时图。「谁上屏」的注入优先级
（置顶显示 > 推送到显示 > 时段）留在内容清单一处（routers/images.py
的 /content），本模块只承载置顶显示的渲染命名空间（ADR-0002）。

文件协议（三件套，均在 upload_dir 下）：
  display.fps6      当前帧（FPS6 设备格式；帧头解析校验统一走 parse_fps6，
                    损坏不可解析时视同无置顶显示）
  display.json      元数据（filename / landscape / has_orig / ts）
  display.orig.jpg  原图缩略图（量化前，供管理台「当前显示」预览，省流量）

id 是内容指纹（display- + 帧字节 sha256 前 10 位），仅供设备变更检测，
不是查找键：服务端一律返回当前帧，不按指纹寻址。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from app import db
from app.config import settings
from app.epd_image import is_landscape, make_thumbnail, parse_fps6

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(settings.upload_dir)
DISPLAY_FILE = UPLOAD_DIR / "display.fps6"
DISPLAY_META = UPLOAD_DIR / "display.json"
# 原始图缩略图（量化前，供管理台「当前显示」预览，省流量）
DISPLAY_ORIG = UPLOAD_DIR / "display.orig.jpg"


def current_meta() -> dict | None:
    """置顶显示图元数据（含内容指纹）；无则 None。

    帧头解读统一走 parse_fps6（内部自检口径，不校验尺寸）：
    DISPLAY_FILE 损坏不可解析时视同无置顶显示（回落时段自动内容）。
    不含 url/preview_url——源不负责 url（ADR-0002），清单与「当前显示」
    的 url 由路由层 helper _item_urls 统一组装为 /{id}/raw|preview 形态
    （固定 /display/raw|preview 路由已退役，T7 #17）。
    """
    if not DISPLAY_FILE.exists():
        return None
    m = {}
    if DISPLAY_META.exists():
        try:
            m = json.loads(DISPLAY_META.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            m = {}
    raw = DISPLAY_FILE.read_bytes()
    try:
        prepared = parse_fps6(raw)
    except ValueError as exc:
        logger.warning("display frame unreadable, treating as no manual display: %s", exc)
        return None
    content_id = "display-" + hashlib.sha256(raw).hexdigest()[:10]
    return {
        "id": content_id,
        "filename": m.get("filename", ""),
        "landscape": 1 if m.get("landscape") else 0,
        "width": prepared.width,
        "height": prepared.height,
        "has_orig": bool(m.get("has_orig")),
    }


def save_upload(raw: bytes, prepared, filename: str) -> dict | None:
    """普通临时上传：写帧 + 原图缩略图 + 元数据，返回 current_meta()。

    量化（prepare_image）由调用方完成；本函数只落三件套。
    """
    DISPLAY_FILE.write_bytes(prepared.data)
    _write_orig_thumbnail(raw)
    _write_meta(filename=filename, landscape=1 if is_landscape(raw) else 0,
                has_orig=True)
    return current_meta()


def save_raw_fps6(raw: bytes, filename: str) -> dict | None:
    """直推已量化 FPS6（校准链路等）：原样写帧，不重新量化。

    直推帧本就没有原图，顺带清掉旧缩略图，避免预览误显示上一次
    上传的原图。
    """
    DISPLAY_FILE.write_bytes(raw)
    DISPLAY_ORIG.unlink(missing_ok=True)
    # 校准图按横放视角设计，设备横放观看
    _write_meta(filename=filename, landscape=1, has_orig=False)
    return current_meta()


def save_pushed_frame(raw: bytes, filename: str) -> dict | None:
    """工具链直推（校准图 / 彩虹图）：原样写帧与元数据（沿用既有 json 形态）。"""
    DISPLAY_FILE.write_bytes(raw)
    DISPLAY_META.write_text(json.dumps({
        "filename": filename,
        "landscape": 1,
        "ts": db.now(),
    }, ensure_ascii=False), encoding="utf-8")
    return current_meta()


def read_frame() -> bytes | None:
    """当前帧字节；无置顶显示返回 None。"""
    if not DISPLAY_FILE.exists():
        return None
    return DISPLAY_FILE.read_bytes()


def read_original_thumbnail() -> bytes | None:
    """原图缩略图字节（display.orig.jpg）；不存在返回 None。"""
    if DISPLAY_ORIG.exists():
        try:
            return DISPLAY_ORIG.read_bytes()
        except OSError:
            return None
    return None


def clear() -> None:
    """清除置顶显示（帧 / 元数据 / 原图缩略图三件套），恢复时段自动内容。"""
    for p in (DISPLAY_FILE, DISPLAY_META, DISPLAY_ORIG):
        if p.exists():
            p.unlink()


def _write_orig_thumbnail(raw: bytes) -> None:
    """把原始图片转成缩略图保存（最长边 ~500px，JPEG 80），失败时静默。"""
    try:
        DISPLAY_ORIG.write_bytes(make_thumbnail(raw))
    except Exception:
        try:
            DISPLAY_ORIG.unlink(missing_ok=True)
        except OSError:
            pass


def _write_meta(filename: str, landscape: int, has_orig: bool) -> None:
    DISPLAY_META.write_text(json.dumps({
        "filename": filename,
        "landscape": landscape,
        "has_orig": has_orig,
        "ts": db.now(),
    }, ensure_ascii=False), encoding="utf-8")


# ═══════════════════════ 内容源适配器（ADR-0002） ═══════════════════════

class DisplaySource:
    """置顶显示内容源：display- 前缀的渲染命名空间（当前内容 = 唯一临时图）。

    id 是当前帧字节的内容指纹（设备变更检测用），不是查找键：
    meta/render/original 一律服务「当前内容」。
    """

    id_prefix = "display-"
    missing_detail = "no display image"

    def meta(self) -> dict | None:
        """返回当前置顶显示的清单字段；无置顶显示返回 None。

        清单字段不含 url/preview_url（源不负责 url，见 ADR-0002）。
        """
        return current_meta()

    def render(self, content_id: str) -> bytes | None:
        """当前置顶显示的 FPS6 帧；无则 None（路由层 404）。"""
        return read_frame()

    def original(self, content_id: str) -> bytes | None:
        """当前置顶显示的原图（量化前缩略图）字节；直推帧无原图返回 None。"""
        if not DISPLAY_FILE.exists():
            return None
        return read_original_thumbnail()


SOURCE = DisplaySource()
