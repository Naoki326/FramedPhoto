"""
analysis.py — 照片分析结果查询（AI 评分 / 每日精选元数据）+ 照片库上传入口。

管理台照片库 tab 化（#25）：/scores 支持按分类过滤 + 返回分类汇总
（名称 + 照片数，供 tab 排序与空分类合并显示）；未分类单列。

家庭共享上传（Frame_* 文件夹）：POST /upload 按文件夹上传照片到照片库
（目标文件夹即分类），后台自动启发式分析（免费）并异步推回 NAS 备份——
家人不用碰 File Station / Synology Photos，浏览器拖一下即可。
"""
import logging
import re
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile

from app import category, db, daily, nas_sync
from app.analyzer import analyze_image
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

UNCATEGORIZED = category.UNCATEGORIZED

# 与同步白名单（sync_nas_filters.sh）一致：只收静态图片，拒绝其它一切
_IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|heic|heif|webp|bmp|tiff?)$", re.IGNORECASE)
# 文件夹名：中文/字母/数字/下划线/横线，无路径分隔符（防穿越）
_FOLDER_NAME = re.compile(r"^[\w\-\u4e00-\u9fff]{1,64}$")


def _sort_shot_at(rows: list[dict]) -> list[dict]:
    """按拍摄时间降序（缺失时间排后）。"""
    return sorted(rows, key=lambda r: (r.get("shot_at") is None, r.get("shot_at") or 0),
                  reverse=True)


def _safe_relpath(rel: str) -> Path:
    """清洗上传相对路径（webkitRelativePath / 前端提供的 paths）：
    拒绝绝对路径与 ..，逐段非空，返回相对 Path；非法抛 ValueError。"""
    rel = (rel or "").replace("\\", "/").strip()
    if not rel or rel.startswith("/"):
        raise ValueError(f"非法路径: {rel!r}")
    parts = []
    for seg in rel.split("/"):
        if seg in ("", ".", "..") or "\x00" in seg:
            raise ValueError(f"非法路径段: {seg!r}")
        parts.append(seg)
    p = Path("/".join(parts))
    if len(parts) > 8:
        raise ValueError("路径层级过深")
    return p


@router.post("/upload")
async def upload_photos(
    bg: BackgroundTasks,
    folder: str = Form(...),
    files: list[UploadFile] = File(...),
    paths: str | None = Form(None),
):
    """家庭共享上传：按文件夹上传照片到照片库（目标文件夹即分类）。

    - folder：目标文件夹名（如 Frame_naoki），须匹配 _FOLDER_NAME（防穿越）
    - files：多文件（multipart）；paths：可选，与 files 一一对应的相对路径
      （文件夹上传时保留子目录结构，分类仍为第一层文件夹名）
    - 落盘 PHOTO_LIB_DIR/<folder>/<relpath>；同名文件跳过（幂等）
    - 后台：启发式分析入库（免费，与同步自动分析同一策略）+ 异步推回 NAS 备份
    """
    if not _FOLDER_NAME.fullmatch(folder or ""):
        raise HTTPException(400, f"非法文件夹名: {folder!r}（仅中文/字母/数字/下划线/横线）")
    lib = Path(settings.photo_lib_dir)
    rel_paths = (paths or "").split("|") if paths else []
    if rel_paths and len(rel_paths) != len(files):
        raise HTTPException(400, "paths 与 files 数量不一致")

    saved, skipped, errors = [], [], []
    for i, f in enumerate(files):
        try:
            rel = _safe_relpath(rel_paths[i]) if i < len(rel_paths) and rel_paths[i] \
                else Path(f.filename or "")
            if not _IMAGE_EXT.search(rel.name):
                raise HTTPException(400, f"仅支持图片文件: {rel.name}")
            dest = lib / folder / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                skipped.append(str(rel))
                continue
            data = await f.read()
            if not data:
                raise HTTPException(400, f"空文件: {rel.name}")
            dest.write_bytes(data)
            saved.append(str(rel))
            # 启发式分析（免费）：与 NAS 同步自动分析同一策略；VLM 深度评分仍手动
            bg.add_task(_analyze_uploaded, str(dest))
        except HTTPException:
            raise
        except (ValueError, OSError) as e:
            errors.append(f"{getattr(f, 'filename', '?')}: {e}")
    if errors and not saved:
        raise HTTPException(400, "; ".join(errors))
    # 有新文件 → 请求级推一次 NAS（备份；每请求一次 rsync，非每张）
    if saved:
        bg.add_task(_push_frame_folder, folder)
    return {"ok": True, "folder": folder, "saved": len(saved), "skipped": len(skipped),
            "files": saved, "skipped_files": skipped, "errors": errors}


def _analyze_uploaded(path: str) -> None:
    """后台任务：启发式分析入库（ADR-0005 哈希迁移语义）。"""
    try:
        a = analyze_image(path, use_vlm=False)
        category.migrate_or_record(a)
    except Exception as exc:  # noqa: BLE001
        logger.exception("frame upload analyze failed: %s", exc)


def _push_frame_folder(folder: str) -> None:
    """后台任务：把照片库文件夹增量推回 NAS 备份（失败不影响上传结果）。"""
    try:
        nas_sync.push_frame_dir(folder)
    except Exception as exc:  # noqa: BLE001
        logger.exception("frame upload push to NAS failed: %s %s", folder, exc)


@router.get("/scores")
async def list_scores(limit: int = Query(50, ge=1, le=500), cat: str | None = Query(None, alias="category"),
                     sort: str = Query("memory", pattern=r"^(memory|shot_at)$")):
    """照片评分列表；可选按分类过滤（cat=None=全部）。

    cat=未知分类名（非现存分类）→ 返回空列表（前端据此显示空 tab）。
    返回 `summary`（分类汇总，含名称+照片数，供管理台 tab 排序）；未分类单列。
    sort=memory（回忆度降序，默认）| shot_at（拍摄时间降序）。
    """
    if cat == UNCATEGORIZED:
        rows = _sort_shot_at(category.uncategorized_photos()) if sort == "shot_at" \
            else category.uncategorized_photos()
    elif cat:
        rows = db.list_photo_scores(limit=limit, category=cat)
        if sort == "shot_at":
            rows = _sort_shot_at(rows)
    else:
        rows = _sort_shot_at(db.list_photo_scores(limit=limit)) if sort == "shot_at" \
            else db.list_photo_scores(limit=limit)
    return {"scores": rows[:limit], "summary": category.category_summary()}


@router.get("/daily")
async def daily_meta():
    """今日精选元数据（当前照片时段；无则 null）。"""
    meta = daily.load_daily_meta()
    return {"daily": meta}


@router.get("/preview")
async def photo_preview(path: str):
    """照片缩略图（管理台照片库网格用）。仅允许 photo_lib_dir / uploads 内文件。

    磁盘缓存：预览图生成很贵（照片库多为 20MP+ 大图，解码+缩略+JPEG 约百毫秒），
    无缓存时 404 张网格每次滚动都重新解码，页面严重卡顿。缓存键 = 路径哈希 +
    文件 mtime（NAS 同步更新照片后 mtime 变化自动重生成，不残留过期缩略图）。
    """
    import hashlib
    import io
    from pathlib import Path as P
    from PIL import Image
    from fastapi.responses import Response

    root = P(settings.photo_lib_dir).resolve()
    upload_root = P(settings.upload_dir).resolve()
    p = P(path).resolve()
    allowed = str(p).startswith(str(root)) or str(p).startswith(str(upload_root))
    if not allowed or not p.is_file():
        raise HTTPException(404, "photo not found")
    try:
        st = p.stat()
        key = hashlib.sha1(f"{p}:{st.st_size}:{int(st.st_mtime)}".encode()).hexdigest()[:20]
        cache_dir = P(settings.upload_dir).parent / "preview_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{key}.jpg"
        if cache_file.is_file():
            return Response(content=cache_file.read_bytes(), media_type="image/jpeg")
        # 大图先 draft 降采样再解码，避免全量解码 20MP 原图（几 MB→几百 KB）
        img = Image.open(p)
        img.draft("RGB", (600, 800))
        img.load()
        img.thumbnail((300, 400))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=80)
        data = buf.getvalue()
        try:
            cache_file.write_bytes(data)
        except OSError:
            pass
        return Response(content=data, media_type="image/jpeg")
    except Exception:
        raise HTTPException(422, "bad image")
