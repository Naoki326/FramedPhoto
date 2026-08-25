"""
daily.py — 每日精选：历史上的今天选片 + 文案渲染（每照片时段各一份）。

从全局「每天一份」改为「每照片时段各一份」（ADR-0006 / #25）：
  - 选片与渲染缓存按「时段/分类维度」键控（slot_key），每个照片时段独立
    选片、独立渲染、独立落盘；used_at 加权去重仍照片级全局共享，天然实现
    同分类多时段不同图。
  - 时段绑定分类后：三级降级（严格不出圈，ADR-0006）
      ①月-日匹配 + 回忆度门槛 + EXIF 来源（限分类内）
      ②分类内按回忆度降序
      ③分类内任意
    三级皆空（分类 0 张）→ 该时段无内容，设备保持上一屏，管理台警告。
  - 未绑定分类的照片时段选片池为全库（含未分类），现有行为不变。
  - 手动指定（管理台「设为今日精选」）全局优先于一切照片时段的自动选片。

文件协议：<DAILY_DIR>/<slot_key>.fps6 与 <slot_key>.json（每时段各一份）。
旧单文件形态（daily.fps6 / daily.json）为兼容残存，新代码一律走 slot_key。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from pathlib import Path

from app import category, db, slots
from app.analyzer import generate_random_caption
from app.config import settings
from app.epd_image import is_landscape, prepare_image_with_caption

DAILY_DIR = Path(settings.upload_dir).parent / "daily"
DAILY_FILE = DAILY_DIR / "daily.fps6"          # 兼容残存（旧单文件形态）
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


# ═══════════════════════ 选片（主 seam，S1） ═══════════════════════

def select_daily_photo(category: str | None = None) -> tuple[dict | None, bool]:
    """选片。返回 (photo, is_today_match)。

    category=None（未绑定分类）：选片池为全库（含未分类），沿用既有两档
    （历史上的今天 → 全局高分未用）。
    category 非空（绑定分类，ADR-0006 不出圈）：分类内三级降级
    （①月-日匹配+门槛+EXIF ②分类内回忆度 ③分类内任意）；三级皆空
    （分类 0 张）返回 (None, False)，该时段无内容。

    第二个返回值供调用方决定是否展示拍摄日期（降级时展示日期会误导）。
    """
    m, d = _today_md()
    if category:
        return _select_scoped(m, d, category)
    return _select_unbounded(m, d)


def _path_exists(c: dict) -> bool:
    """照片原图文件仍存在（文件已删的记录不参与选片）。"""
    try:
        return Path(c.get("path", "")).is_file()
    except (OSError, TypeError):
        return False


def _is_uncat(category_name: str | None) -> bool:
    """分类是否为「未分类」（含 legacy 空串记录的单一口径）。"""
    return category_name == category.UNCATEGORIZED


def _scoped_records(category_name: str, analyzed_only: bool = True) -> list[dict]:
    """分类内全部记录（未分类含 legacy 空串口径；其余按 category 精确匹配）。"""
    if _is_uncat(category_name):
        return category.uncategorized_photos()
    return db.list_photo_scores(limit=200, category=category_name, analyzed_only=analyzed_only)


def _select_scoped(m: int, d: int, category: str) -> tuple[dict | None, bool]:
    """绑定分类的选片：三级降级，严格限于分类内（ADR-0006 不出圈）。"""
    if _is_uncat(category):
        return _select_uncat(m, d)
    # ① 月-日匹配 + 回忆度门槛 + EXIF 来源（限分类内）
    cand = [c for c in db.select_daily_candidates((m, d), settings.daily_min_score,
                                                  limit=30, category=category)
            if _path_exists(c)]
    if cand:
        chosen = _weighted_choice(cand)
        if chosen:
            return chosen, True
    # ② 分类内按回忆度降序（未用优先）
    scoped = [c for c in _scoped_records(category) if _path_exists(c)]
    if not scoped:
        # ③ 分类内任意——分类确有照片但全为 0 分时兜底
        scoped = [c for c in _scoped_records(category, analyzed_only=False) if _path_exists(c)]
    fresh = [c for c in scoped if (c.get("used_at") or 0) < db.now() - 24 * 3600]
    pool = fresh or scoped
    if not pool:
        return None, False   # 分类 0 张：无内容
    top = sorted(pool, key=lambda c: -(c.get("memory_score") or 0))[:10]
    chosen = _weighted_choice(top)
    if chosen:
        return chosen, False
    return None, False


def _select_uncat(m: int, d: int) -> tuple[dict | None, bool]:
    """绑定未分类的选片：未分类池内三级降级（含 legacy 空串口径），严格不出圈。"""
    cand = [c for c in category.uncategorized_photos()
            if c.get("shot_source") == "exif" and c.get("shot_at")
            and dt.datetime.fromtimestamp(c["shot_at"]).month == m
            and dt.datetime.fromtimestamp(c["shot_at"]).day == d
            and (c.get("memory_score") or 0) >= settings.daily_min_score
            and _path_exists(c)]
    if cand:
        chosen = _weighted_choice(cand)
        if chosen:
            return chosen, True
    scoped = [c for c in category.uncategorized_photos() if _path_exists(c)]
    fresh = [c for c in scoped if (c.get("used_at") or 0) < db.now() - 24 * 3600]
    pool = fresh or scoped
    if not pool:
        return None, False
    top = sorted(pool, key=lambda c: -(c.get("memory_score") or 0))[:10]
    chosen = _weighted_choice(top)
    if chosen:
        return chosen, False
    return None, False


def _select_unbounded(m: int, d: int) -> tuple[dict | None, bool]:
    """未绑定分类（全库）选片：沿用既有两档（历史 → 全局高分未用）。"""
    cand = [c for c in db.select_daily_candidates((m, d), settings.daily_min_score, limit=30)
            if _path_exists(c)]
    if cand:
        chosen = _weighted_choice(cand)
        if chosen:
            return chosen, True
    all_ = [c for c in db.list_photo_scores(limit=200) if _path_exists(c)]
    fresh = [c for c in all_ if (c.get("used_at") or 0) < db.now() - 24 * 3600]
    pool = fresh or all_
    top = sorted(pool, key=lambda c: -(c.get("memory_score") or 0))[:10]
    return _weighted_choice(top), False


def daily_manual_pick() -> tuple[dict, bool] | None:
    """管理台手动指定的今日照片（当日有效）。

    返回 (photo, is_today_match)；手动指定即当日生效，恒为当日匹配。
    全局优先于一切照片时段的自动选片（ADR-0006 显式例外）。
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


def current_photo_category() -> str | None:
    """当前照片时段绑定的分类；None = 未绑定（全库选片）。非照片时段返回 None。"""
    seg = slots.current_segment()
    if seg and seg.get("type") == slots.SLOT_PHOTO:
        cat = seg.get("category")
        return cat or None
    return None


def slot_key() -> str:
    """当前照片时段的缓存键：段开始时刻 + 分类（稳定、每日独立、跨午夜延续）。

    无照片时段（管理台等）回退自然日 0 点 + 当前分类（或 all）。
    """
    cat = current_photo_category()
    base = slots.segment_start(typ=slots.SLOT_PHOTO, category=cat)
    if base is None:
        base = dt.datetime.combine(dt.date.today(), dt.time(0, 0))
    label = cat or "all"
    return f"{base:%Y%m%d%H%M}-{label}"


def _files(key: str) -> tuple[Path, Path]:
    """某 slot_key 的帧 / 元数据文件路径。"""
    return DAILY_DIR / f"{key}.fps6", DAILY_DIR / f"{key}.json"


# ═══════════════════════ 渲染 ═══════════════════════

def _render_one(key: str) -> dict | None:
    """渲染某个 slot_key 的每日精选 → FPS6（带文案）→ 返回元数据；无照片返回 None。"""
    manual = daily_manual_pick()
    if manual is not None:
        photo, is_today_match = manual
    else:
        photo, is_today_match = select_daily_photo(category=current_photo_category())
    if not photo:
        return None
    path = photo["path"]
    if not Path(path).exists():
        db.upsert_photo_score(photo["path"], analyzed_at=None)  # 标记失效重扫
        return None

    caption = generate_random_caption(path)
    if not caption:
        caption = photo.get("caption") or ""
    shot_at = photo.get("shot_at")
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
    frame_file, meta_file = _files(key)
    frame_file.write_bytes(prepared.data)
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
        "landscape": 1 if is_landscape(raw) else 0,
        "rendered_at": db.now(),
        "key": key,
        "category": current_photo_category(),
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    db.mark_photo_used(photo["path"], db.now())
    return meta


def render_daily() -> dict | None:
    """渲染当前照片时段的每日精选（旧单文件兼容入口，走当前 slot_key）。"""
    return _render_one(slot_key())


def render_daily_for(key: str) -> dict | None:
    """按 slot_key 渲染；无可用照片返回 None。"""
    return _render_one(key)


def _is_fresh_for(key: str, meta: dict | None) -> bool:
    """slot_key 的每日精选是否已生成（按自然日判断：渲染日期为今天才算新鲜）。"""
    if meta is None or not meta.get("rendered_at"):
        return False
    today_str = dt.datetime.now().strftime("%Y%m%d")
    rendered_str = dt.datetime.fromtimestamp(meta["rendered_at"]).strftime("%Y%m%d")
    if rendered_str != today_str:
        return False
    manual = daily_manual_pick()
    if manual is not None:
        manual_photo, _ = manual
        if meta.get("path") != manual_photo["path"]:
            return False
    return True


def daily_is_fresh(ttl_seconds: int = 20 * 3600) -> bool:
    """当前照片时段每日精选是否已生成（旧单文件兼容入口）。"""
    frame, meta_file = _files(slot_key())
    if not (frame.exists() and meta_file.exists()):
        return False
    return _is_fresh_for(slot_key(), _load_meta_file(meta_file))


def _load_meta_file(meta_file: Path) -> dict | None:
    if not meta_file.exists():
        return None
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_daily_meta() -> dict | None:
    """当前照片时段的每日精选元数据（旧单文件兼容入口，走当前 slot_key）。"""
    _frame, meta_file = _files(slot_key())
    return _load_meta_file(meta_file)


def load_daily_meta_for(key: str) -> dict | None:
    _frame, meta_file = _files(key)
    return _load_meta_file(meta_file)


def ensure_daily() -> dict | None:
    """确保当前照片时段的每日精选已生成，返回其元数据。"""
    key = slot_key()
    if daily_is_fresh():
        meta = load_daily_meta()
        if meta:
            return meta
    return render_daily_for(key)


def ensure_daily_for(key: str) -> dict | None:
    """按 slot_key 确保已生成，返回其元数据。"""
    _frame, meta_file = _files(key)
    meta = _load_meta_file(meta_file)
    if _is_fresh_for(key, meta):
        return meta
    return render_daily_for(key)


def daily_fps6_exists() -> bool:
    """当前照片时段的 FPS6 帧是否存在（旧单文件兼容入口）。"""
    frame, _meta = _files(slot_key())
    return frame.exists()


def daily_fps6_exists_for(key: str) -> bool:
    frame, _meta = _files(key)
    return frame.exists()


# ═══════════════════════ 内容源适配器（ADR-0002） ═══════════════════════

class DailySource:
    """每日精选内容源：daily- 前缀的渲染命名空间。

    id 是当日渲染帧的内容指纹（设备变更检测用），不是查找键：
    meta/render/original 一律服务「当前照片时段」的当日精选。
    """

    id_prefix = "daily-"
    missing_detail = "no daily photo"

    def meta(self) -> dict | None:
        """确保当前照片时段的每日精选已生成，返回内容清单字段；无照片返回 None。"""
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
        """当前照片时段的 FPS6 帧（必要时渲染；无可用照片返回 None）。"""
        key = slot_key()
        if not daily_fps6_exists_for(key):
            ensure_daily_for(key)
        if not daily_fps6_exists_for(key):
            return None
        frame, _meta = _files(key)
        return frame.read_bytes()

    def original(self, content_id: str) -> bytes | None:
        """当前照片时段原图（选中照片的原始文件字节）；缺失返回 None。"""
        path = (ensure_daily() or {}).get("path")
        if path and Path(path).is_file():
            try:
                return Path(path).read_bytes()
            except OSError:
                return None
        return None


SOURCE = DailySource()
