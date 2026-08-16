"""
daily.py — 每日精选：历史上的今天选片 + 文案渲染。

流程：
  1. select_daily_photo(): 按今天"月-日"匹配所有年份的照片（需有 EXIF
     拍摄时间），回忆度 ≥ 阈值，随机加权（最近用过的降权）避免重复
  2. 无历史上的今天候选时：降级为"全局高分未用照片"
  3. render_daily(): 选中的照片叠加文案（底部圆角衬底块 + 白字）→ FPS6
     → 存入每日精选目录，content 接口优先返回

手动指定（管理台「设为今日精选」）优先于自动选片；上传图经内容库
「推送到显示」或被选为每日精选上屏（时段编排见 app/slots.py）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from pathlib import Path

from app import db
from app.analyzer import generate_random_caption
from app.config import settings
from app.epd_image import is_landscape, prepare_image_with_caption

DAILY_DIR = Path(settings.upload_dir).parent / "daily"
DAILY_FILE = DAILY_DIR / "daily.fps6"
DAILY_META = DAILY_DIR / "daily.json"


def _today_md() -> tuple[int, int]:
    today = dt.date.today()
    return today.month, today.day


def _weighted_choice(candidates: list[dict]) -> dict | None:
    """按最近使用时间加权随机（越久未用权重越高）。"""
    now = db.now()
    weights = []
    for c in candidates:
        used = c.get("used_at") or 0
        days_ago = (now - used) / 86400.0
        weight = 1.0 + max(0, days_ago)  # 未用过 weight=1，用过则随时间回升
        weights.append(weight)
    total = sum(weights)
    r = random.uniform(0, total)
    acc = 0.0
    for c, w in zip(candidates, weights):
        acc += w
        if r <= acc:
            return c
    return candidates[-1] if candidates else None


def select_daily_photo() -> tuple[dict | None, bool]:
    """选片。返回 (photo, is_today_match)。

    策略：历史上的今天（EXIF 月-日匹配今日）→ 无则降级全局高分未用。
    第二个返回值供调用方决定是否展示拍摄日期（降级时展示日期会误导）。
    """
    m, d = _today_md()
    cand = db.select_daily_candidates((m, d), settings.daily_min_score, limit=30)
    if cand:
        chosen = _weighted_choice(cand)
        if chosen:
            return chosen, True

    # 降级：全局高分、未用优先
    all_ = db.list_photo_scores(limit=200)
    fresh = [c for c in all_ if (c.get("used_at") or 0) < db.now() - 24 * 3600]
    pool = fresh or all_
    top = sorted(pool, key=lambda c: -(c.get("memory_score") or 0))[:10]
    return _weighted_choice(top), False


def daily_manual_pick() -> tuple[dict, bool] | None:
    """管理台手动指定的今日照片（当日有效）。

    返回 (photo, is_today_match)；手动指定即当日生效，恒为当日匹配。
    """
    from app.runtime_config import load
    m = load().get("daily_manual") or {}
    if not m.get("path"):
        return None
    today = dt.datetime.now().strftime("%Y-%m-%d")
    if m.get("date") != today:
        return None
    photo = db.get_photo_score(m["path"]) or db.get_photo_score(str(Path(m["path"]).resolve()))
    if not photo or not Path(photo["path"]).exists():
        return None
    return photo, True


def render_daily() -> dict | None:
    """选片 → 渲染 FPS6（带文案）→ 返回元数据；无可用照片返回 None。

    手动指定（管理台「设为今日精选」）优先，其次自动选片。
    """
    manual = daily_manual_pick()
    if manual is not None:
        photo, is_today_match = manual
    else:
        photo, is_today_match = select_daily_photo()
    if not photo:
        return None
    path = photo["path"]
    if not Path(path).exists():
        db.upsert_photo_score(photo["path"], analyzed_at=None)  # 标记失效重扫
        return None

    # 随机文案：每次渲染（每天）按需调用 VLM 生成，角度随机；失败回退 DB 已有文案
    caption = generate_random_caption(path)
    if not caption:
        caption = photo.get("caption") or ""
    shot_at = photo.get("shot_at")
    # 仅“历史上的今天”匹配时展示拍摄日期；降级选片不展示日期（避免误导）
    date_str = ""
    if is_today_match and shot_at:
        try:
            date_str = dt.datetime.fromtimestamp(shot_at).strftime("%Y.%m.%d")
        except (ValueError, OSError):
            date_str = ""

    raw = Path(path).read_bytes()
    prepared = prepare_image_with_caption(
        raw, caption=caption, date_str=date_str,
        width=settings.epd_width, height=settings.epd_height,
    )

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_FILE.write_bytes(prepared.data)
    # 内容指纹：文件变化时 id 变化，设备端据此感知“内容已更新”重新拉取
    content_id = "daily-" + hashlib.sha256(prepared.data).hexdigest()[:10]
    meta = {
        "id": content_id,
        "path": photo["path"],
        "filename": photo.get("filename", ""),
        "caption": caption,
        "date": date_str,
        "memory_score": photo.get("memory_score"),
        "width": prepared.width,
        "height": prepared.height,
        "landscape": 1 if is_landscape(raw) else 0,  # Web 预览摆正用
        "rendered_at": db.now(),
    }
    DAILY_META.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    db.mark_photo_used(photo["path"], db.now())
    return meta


def daily_is_fresh(ttl_seconds: int = 20 * 3600) -> bool:
    """今日精选是否已生成（按自然日判断：渲染日期为今天才算新鲜）。

    每天 0 点后第一次调用即重新渲染，实现“每天换一张”。
    手动指定今日精选后若与当前渲染不一致，强制重渲染。
    """
    if not (DAILY_FILE.exists() and DAILY_META.exists()):
        return False
    meta = load_daily_meta()
    if not meta:
        return False
    rendered = meta.get("rendered_at")
    if not rendered:
        return False
    today_str = dt.datetime.now().strftime("%Y%m%d")
    rendered_str = dt.datetime.fromtimestamp(rendered).strftime("%Y%m%d")
    if rendered_str != today_str:
        return False
    # 手动指定与当前渲染不一致 → 需要重渲染
    manual = daily_manual_pick()
    if manual is not None:
        manual_photo, _is_match = manual
        if meta.get("path") != manual_photo["path"]:
            return False
    return True


def load_daily_meta() -> dict | None:
    if not DAILY_META.exists():
        return None
    try:
        return json.loads(DAILY_META.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def ensure_daily() -> dict | None:
    """确保今天的每日精选已生成，返回其元数据。"""
    if daily_is_fresh():
        meta = load_daily_meta()
        if meta:
            return meta
    return render_daily()

def daily_fps6_exists() -> bool:
    return DAILY_FILE.exists()


# ═══════════════════════ 内容源适配器（ADR-0002） ═══════════════════════

class DailySource:
    """每日精选内容源：daily- 前缀的渲染命名空间。

    id 是当日渲染帧的内容指纹（设备变更检测用），不是查找键：
    meta/render/original 一律服务「当前内容」（当日精选）。
    """

    id_prefix = "daily-"
    missing_detail = "no daily photo"

    def meta(self) -> dict | None:
        """确保当日精选已生成，返回内容清单字段；无可用照片返回 None。"""
        m = ensure_daily()
        if not m:
            return None
        return {
            "id": m.get("id", "daily"),
            "filename": m.get("filename", ""),
            "caption": m.get("caption", ""),
            "date": m.get("date", ""),
            "memory_score": m.get("memory_score"),
            "width": m["width"],
            "height": m["height"],
            "landscape": m.get("landscape") or 0,
        }

    def render(self, content_id: str) -> bytes | None:
        """当日精选的 FPS6 帧（必要时渲染；无可用照片返回 None）。"""
        if not daily_fps6_exists():
            render_daily()
        if not daily_fps6_exists():
            return None
        return DAILY_FILE.read_bytes()

    def original(self, content_id: str) -> bytes | None:
        """当日精选原图（选中照片的原始文件字节）；缺失返回 None。"""
        path = (ensure_daily() or {}).get("path")
        if path and Path(path).is_file():
            try:
                return Path(path).read_bytes()
            except OSError:
                return None
        return None


SOURCE = DailySource()
