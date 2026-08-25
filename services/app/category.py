"""
category.py — 照片分类：NAS photo 第一层文件夹名（ADR-0005 / 0007）。

一张照片恰好属于一个分类：分类由路径第一段推导（无目录分隔符 = 未分类），
深层子目录归其第一层祖先。分类没有独立于照片的生命周期——分类列表由照片
记录聚合而来（空分类的 tab 由磁盘目录列表补足）。

NAS /volume1/photo 是唯一共享照片目录（不做每人一夹）：文件夹经管理台
新建 / 重命名 / 删除空文件夹（ADR-0007）——先在 NAS 侧执行，成功后同步
本地镜像并迁移评分记录；上传也不再按人命名，统一进所选文件夹或根目录。

内容哈希迁移（ADR-0005）：分析扫描发现新路径时按内容哈希查既有记录——
命中且旧路径文件已不存在 → 迁移（更新 path 与分类，保留评分文案）；
命中但旧文件还在（复制）→ 并存为两条记录；不命中 → 正常新分析。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from app import db, nas_sync
from app.config import settings

# 未分类的特殊标记（照片库根目录散照 / 内容库上传图所属分类，正常参与选片）
UNCATEGORIZED = "未分类"

# 文件夹名：中文/字母/数字/下划线/横线，无路径分隔符（防穿越）；
# 与上传端点共用同一套规则（routers/analysis.py）
FOLDER_NAME = re.compile(r"^[\w\-\u4e00-\u9fff]{1,64}$")


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
    """照片库根目录第一层文件夹名（补足空分类 tab）。"""
    root = Path(settings.photo_lib_dir)
    try:
        if not root.is_dir():
            return []
        return sorted(d.name for d in root.iterdir() if d.is_dir())
    except OSError:
        return []


# ---------- 文件夹管理（ADR-0007：NAS 侧新建 / 重命名 / 删除空文件夹） ----------

def valid_folder_name(name: str) -> bool:
    """文件夹名合法性：字符集 / 长度 / 非保留名（未分类）。"""
    return bool(FOLDER_NAME.fullmatch(name or "")) and name != UNCATEGORIZED


def folder_path(name: str) -> Path:
    return Path(settings.photo_lib_dir) / name


def folder_exists(name: str) -> bool:
    return folder_path(name).is_dir()


def folder_photo_count(name: str) -> int:
    """文件夹下实际图片文件数（递归；不只已分析的记录）。"""
    exts = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp", ".bmp", ".tiff", ".tif"}
    try:
        return sum(1 for p in folder_path(name).rglob("*") if p.is_file() and p.suffix.lower() in exts)
    except OSError:
        return 0


def list_folders() -> list[dict]:
    """照片库文件夹列表（本地镜像第一层目录 + 实际照片数），按名称排序。

    镜像可能滞后于 NAS（尚未同步）：以磁盘目录为准，镜像缺失时回退
    NAS 实际列表（count 记 0，同步后自然补齐）。
    """
    names = _disk_first_level_dirs()
    try:
        if not names:
            names = nas_sync.remote_list_dirs()
    except Exception:  # noqa: BLE001 — NAS 不可达时退回镜像事实
        pass
    return [{"name": n, "count": folder_photo_count(n)} for n in names]


def create_folder(name: str) -> Path:
    """在 NAS photo 下新建空文件夹（成为新分类 tab），成功后镜像到本地。

    名字非法 / 重名 / NAS 不可达抛异常；NAS 先行，本地镜像不超前于 NAS。
    """
    if not valid_folder_name(name):
        raise ValueError(f"非法文件夹名: {name!r}（仅中文/字母/数字/下划线/横线，且不可叫「{UNCATEGORIZED}」）")
    if folder_exists(name):
        raise FileExistsError(f"文件夹已存在: {name}")
    nas_sync.remote_mkdir(name)   # NAS 先建；失败（已存在/不可达）直接抛
    path = folder_path(name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def rename_folder(old: str, new: str) -> int:
    """重命名文件夹：NAS mv → 本地镜像 mv → 迁移评分记录（ADR-0007）。

    返回迁移的评分记录条数。名字非法 / 源不存在 / 目标重名 / NAS 不可达
    抛异常。编排层联动（时段绑定 / 手动指定今日精选）由路由层负责。
    """
    if not valid_folder_name(new):
        raise ValueError(f"非法文件夹名: {new!r}（仅中文/字母/数字/下划线/横线，且不可叫「{UNCATEGORIZED}」）")
    if not folder_exists(old):
        raise FileNotFoundError(f"文件夹不存在: {old}")
    if folder_exists(new):
        raise FileExistsError(f"目标文件夹已存在: {new}")
    nas_sync.remote_rename(old, new)   # NAS 先行（内含目标存在探测，防 mv 进目录）
    folder_path(old).rename(folder_path(new))
    return db.rename_category(old, new)


def delete_folder(name: str) -> int:
    """删除空文件夹（NAS 与本地），连带清理评分残留记录，返回清除条数。

    仅允许删除空文件夹（非空抛 OSError）；磁盘残留记录（文件已不在但
    评分还在）随删除一并清掉，避免 tab 幽灵计数。
    """
    if name == UNCATEGORIZED:
        raise ValueError(f"「{UNCATEGORIZED}」不是文件夹，不可删除")
    path = folder_path(name)
    if not path.is_dir():
        raise FileNotFoundError(f"文件夹不存在: {name}")
    if any(path.iterdir()):
        raise OSError(f"文件夹非空，请先清空（移出或删除照片）: {name}")
    nas_sync.remote_rmdir(name)   # NAS 先删（rmdir 只删空目录，双重保险）
    path.rmdir()
    return db.delete_category_scores(name)


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
