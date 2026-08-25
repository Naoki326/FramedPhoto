"""
analysis.py — 照片分析结果查询（AI 评分 / 每日精选元数据）+ 照片库上传与文件夹管理。

管理台照片库 tab 化（#25）：/scores 支持按分类过滤 + 返回分类汇总
（名称 + 照片数，供 tab 排序与空分类合并显示）；未分类单列。

照片上传（ADR-0007）：POST /upload 上传照片到 photo 指定文件夹（目标文件夹
即分类，不存在则自动创建；不传 folder = 根目录/未分类），本地镜像落盘后
后台自动启发式分析（免费）并增量推回 NAS（NAS 是最终存储）。

文件夹管理（ADR-0007）：POST/PATCH/DELETE /folders 在 NAS photo 下
新建 / 重命名 / 删除空文件夹（NAS 先行，本地镜像与评分记录随动），
重命名连带迁移时段绑定与手动指定今日精选。
"""
import logging
import re
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile

from app import category, db, daily, nas_sync, runtime_config, slots
from app.analyzer import analyze_image
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

UNCATEGORIZED = category.UNCATEGORIZED

# 与上传白名单一致：只收静态图片，拒绝其它一切
_IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|heic|heif|webp|bmp|tiff?)$", re.IGNORECASE)
_FOLDER_NAME = category.FOLDER_NAME


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
    folder: str | None = Form(None),
    files: list[UploadFile] = File(...),
    paths: str | None = Form(None),
):
    """上传照片到 photo 指定文件夹（目标文件夹即分类，不存在则自动创建）。

    - folder：目标文件夹名，须匹配 _FOLDER_NAME（防穿越）；缺省/空 = 根目录
      （照片归「未分类」，ADR-0007：不再按人命名，共用同一个 photo 目录）
    - files：多文件（multipart）；paths：可选，与 files 一一对应的相对路径
      （文件夹上传时保留子目录结构，分类仍为第一层文件夹名）
    - 落盘 PHOTO_LIB_DIR/[folder/]<relpath>；同名文件跳过（幂等）
    - 后台：启发式分析入库（免费）+ 增量推回 NAS（最终存储）
    """
    folder = (folder or "").strip()
    if folder and not _FOLDER_NAME.fullmatch(folder):
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
            dest = (lib / folder / rel) if folder else (lib / rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                skipped.append(str(rel))
                continue
            data = await f.read()
            if not data:
                raise HTTPException(400, f"空文件: {rel.name}")
            dest.write_bytes(data)
            saved.append(str(rel))
            # 启发式分析（免费）；VLM 深度评分仍手动
            bg.add_task(_analyze_uploaded, str(dest))
        except HTTPException:
            raise
        except (ValueError, OSError) as e:
            errors.append(f"{getattr(f, 'filename', '?')}: {e}")
    if errors and not saved:
        raise HTTPException(400, "; ".join(errors))
    # 有新文件 → 请求级推一次 NAS（最终存储；每请求一次 rsync，非每张）
    if saved:
        bg.add_task(_push_frame_folder, folder or None)
    return {"ok": True, "folder": folder, "saved": len(saved), "skipped": len(skipped),
            "files": saved, "skipped_files": skipped, "errors": errors}


def _analyze_uploaded(path: str) -> None:
    """后台任务：启发式分析入库（ADR-0005 哈希迁移语义）。"""
    try:
        a = analyze_image(path, use_vlm=False)
        category.migrate_or_record(a)
    except Exception as exc:  # noqa: BLE001
        logger.exception("frame upload analyze failed: %s", exc)


def _push_frame_folder(folder: str | None) -> None:
    """后台任务：把上传的文件夹（None=根目录散照）增量推回 NAS（最终存储）。

    失败不影响上传结果（本地镜像已落盘），下次上传同一文件夹时增量补推。"""
    try:
        nas_sync.push_frame_dir(folder)
    except Exception as exc:  # noqa: BLE001
        logger.exception("frame upload push to NAS failed: %s %s", folder, exc)


# ---------- 文件夹管理（ADR-0007：新建 / 重命名 / 删除空文件夹） ----------

def _rewrite_manual_pick(old: str, new: str | None) -> None:
    """重命名/删除文件夹后同步手动指定今日精选的路径（前缀段替换 / 指向失效则清除）。"""
    m = runtime_config.get("daily_manual") or {}
    path = m.get("path", "")
    if not path:
        return
    parts = path.replace("\\", "/").split("/")
    for i, seg in enumerate(parts):
        if seg == old and i + 1 < len(parts):
            if new is None:
                runtime_config.save({"daily_manual": {}})
            else:
                parts[i] = new
                runtime_config.save({"daily_manual": {**m, "path": "/".join(parts)}})
            return


def _rebind_slot_categories(old: str, new: str | None) -> int:
    """时段绑定分类联动：绑定旧名的段改绑新名（删除时置空回全库）；返回改动段数。"""
    segs = slots.segments()
    changed = 0
    for s in segs:
        if s.get("category") == old:
            s["category"] = new
            changed += 1
    if changed:
        runtime_config.save({"slot_segments": segs})
    return changed


@router.get("/folders")
async def list_folders():
    """照片库文件夹列表（磁盘事实：名称 + 实际照片数，递归含子目录）。"""
    return {"folders": category.list_folders()}


@router.post("/folders")
async def create_folder(body: dict):
    """在 NAS photo 下新建空文件夹（成为新分类 tab，可后续上传照片进去）。"""
    name = (body.get("name") or "").strip()
    try:
        path = category.create_folder(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc   # NAS 不可达 / rsync 异常
    except OSError as exc:
        raise HTTPException(500, f"创建失败: {exc}") from exc
    return {"ok": True, "name": name, "path": str(path)}


@router.patch("/folders/{name}")
async def rename_folder(name: str, body: dict):
    """重命名文件夹：NAS mv → 本地镜像 mv → 迁移评分记录 + 联动时段绑定与手动精选。"""
    new = (body.get("new_name") or "").strip()
    try:
        moved = category.rename_folder(name, new)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc   # NAS 不可达 / mv 失败
    except OSError as exc:
        raise HTTPException(500, f"重命名失败: {exc}") from exc
    _rewrite_manual_pick(name, new)
    rebound = _rebind_slot_categories(name, new)
    return {"ok": True, "old": name, "new": new, "moved_records": moved,
            "rebound_segments": rebound}


@router.delete("/folders/{name}")
async def delete_folder(name: str):
    """删除空文件夹（NAS 与本地，连带清理该分类残留评分记录与时段绑定）。非空 → 409。"""
    try:
        removed = category.delete_folder(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc   # NAS 不可达 / rmdir 失败
    _rewrite_manual_pick(name, None)
    rebound = _rebind_slot_categories(name, None)
    return {"ok": True, "deleted": name, "removed_records": removed,
            "rebound_segments": rebound}


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
    文件 mtime（照片更新后 mtime 变化自动重生成，不残留过期缩略图）。
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
