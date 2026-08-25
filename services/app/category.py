"""
category.py — 照片分类：镜像 NAS 照片库第一层文件夹名（ADR-0005）。

一张照片恰好属于一个分类：分类由路径第一段推导（无目录分隔符 = 未分类），
深层子目录归其第一层祖先。分类没有独立于照片的生命周期——分类列表由照片
记录聚合而来（空分类的 tab 由磁盘目录列表补足）。不做打标 / 手动指派 / 改名。

内容哈希迁移（ADR-0005）：分析扫描发现新路径时按内容哈希查既有记录——
命中且旧路径文件已不存在 → 迁移（更新 path 与分类，保留评分文案）；
命中但旧文件还在（复制）→ 并存为两条记录；不命中 → 正常新分析。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from app import db
from app.config import settings

# 未分类的特殊标记（NAS 照片库根目录散照所属分类，正常参与选片）
UNCATEGORIZED = "未分类"


def derive_category(path: str) -> str:
    """由路径第一段推导分类：根目录散照 → 未分类，深层子目录归第一层祖先。

    以「相对照片库根目录的路径」的第一段为分类：
      - 相对路径无目录分隔符（根目录散照 / 上传图）→ 未分类
      - 有分隔符 → 第一个目录段（一层分类，嵌套再深也只取第一层）
    """
    rel = _relative_to_photo_lib(path)
    if rel is None or rel.parent == Path("."):
        return UNCATEGORIZED
    return rel.parts[0]


def _relative_to_photo_lib(path: str) -> Path | None:
    """把 path 归一为「相对照片库根」的路径；不在照片库下返回 None。"""
    p = Path(path)
    root = Path(settings.photo_lib_dir).resolve()
    try:
        resolved = p.resolve()
        if resolved.is_relative_to(root):
            return resolved.relative_to(root)
    except OSError:
        pass
    # 上传图（uploads/xx.orig）不入照片库：语义上归「未分类」
    if "/uploads/" in path.replace("\\", "/") or path.startswith("uploads/"):
        return Path(".")
    return None


def list_categories() -> list[str]:
    """照片分类列表（非空记录聚合）；只含已存在的分类，不含未分类条目标签。"""
    return db.list_categories()


def uncategorized_photos(sort: str = "memory", analyzed_only: bool = True) -> list[dict]:
    """未分类照片：category 为「未分类」或 legacy 空串的记录（两种存储口径一致）。

    analyzed_only=False 时含未分析记录（③级降级兜底用，US4）。"""
    rows = [r for r in db.list_photo_scores(limit=100000, analyzed_only=analyzed_only)
            if not (r.get("category") or "") or r.get("category") == UNCATEGORIZED]
    if sort == "shot_at":
        rows = sorted(rows, key=lambda r: (r.get("shot_at") is None, r.get("shot_at") or 0),
                      reverse=True)
    return rows


def category_summary() -> list[dict]:
    """分类汇总：名称 + 照片数，按照片数降序（tab 排序用，ADR 需求 12/13）。

    含空分类（0 张）条目由磁盘目录列表补足（ADR 需求 14）；未分类参与降序
    （US12，不无条件排末尾）。counts 来自 db.category_counts——legacy 空串已由
    SQL 归并进未分类（COALESCE(NULLIF(category,''),'未分类')），无需二次统计。
    """
    counts = db.category_counts()
    # 补足空分类：照片库根下第一层文件夹（含无照片的空文件夹），未分类不在此列
    for name in _disk_first_level_dirs():
        counts.setdefault(name, 0)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"name": name, "count": count} for name, count in ordered]


def _disk_first_level_dirs() -> list[str]:
    """照片库根目录第一层文件夹名（补足空分类 tab，忠实镜像 NAS 结构）。"""
    root = Path(settings.photo_lib_dir)
    try:
        if not root.is_dir():
            return []
        return sorted(d.name for d in root.iterdir() if d.is_dir())
    except OSError:
        return []


def compute_content_hash(path: str) -> str:
    """照片内容哈希（迁移识别键）：取原图字节 sha256 前 16 位。"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _analysis_fields(a: "Analysis") -> dict:
    """把 Analysis 转成 upsert 的字段集（不含 path/analyzed_at/category/hash）。"""
    return {
        "filename": a.filename, "caption": a.caption, "description": a.description,
        "type": a.type, "memory_score": a.memory_score, "beauty_score": a.beauty_score,
        "reason": a.reason, "shot_at": a.shot_at, "shot_source": a.shot_source,
        "gps_lat": a.gps_lat, "gps_lon": a.gps_lon, "source": a.source,
    }


def migrate_or_record(a: "Analysis") -> dict:
    """分析入库：按内容哈希识别「移动」与「复制」，决定新记录或迁移（ADR-0005）。

    命中且旧路径文件已不存在 → 迁移（更新 path 与分类，保留评分文案）；
    命中但旧文件还在（复制）→ 并存为两条记录（同图可能在不同分类各上屏）；
    不命中 → 正常新分析。返回最终写库的记录行。
    """
    path = a.path
    content_hash = compute_content_hash(path)
    cat = derive_category(path)
    analyzed_at = db.now()
    hit = db.find_photo_by_hash(content_hash) if content_hash else None
    if hit and content_hash:
        old_path = hit.get("path", "")
        if old_path and old_path != path and not Path(old_path).exists():
            # 移动：旧路径文件已不存在 → 迁移记录到新路径与新分类，保留评分文案
            new = dict(hit)
            new.pop("path", None)   # path 作为 upsert 首参，避免重复传参
            new["category"] = cat
            new["content_hash"] = content_hash
            new["analyzed_at"] = analyzed_at
            new["shot_at"] = a.shot_at
            new["shot_source"] = a.shot_source
            new["gps_lat"] = a.gps_lat
            new["gps_lon"] = a.gps_lon
            db.upsert_photo_score(path, **new)
            db.delete_photo_score(old_path)
            return db.get_photo_score(path) or {}
        # 复制（旧文件还在）或刷新同一路径：按 hit 内容改写新路径记录
        if hit.get("path") == path:
            # 同一路径刷新：只补分类 / 内容哈希，保留既有分析字段
            db.upsert_photo_score(path, category=cat, content_hash=content_hash)
            return db.get_photo_score(path) or {}
        # 复制：并存为两条记录（同图不同分类），分数复制自 hit（不重新分析）
        fields = {k: v for k, v in hit.items() if k not in ("path", "category",
                                                            "content_hash", "analyzed_at")}
        fields.update(filename=a.filename, shot_at=a.shot_at, shot_source=a.shot_source,
                      gps_lat=a.gps_lat, gps_lon=a.gps_lon, source=a.source)
        db.upsert_photo_score(path, **fields, category=cat, content_hash=content_hash,
                              analyzed_at=analyzed_at)
        return db.get_photo_score(path) or {}
    # 不命中：正常新分析
    fields = _analysis_fields(a)
    db.upsert_photo_score(path, **fields, category=cat, content_hash=content_hash,
                          analyzed_at=analyzed_at)
    return db.get_photo_score(path) or {}
