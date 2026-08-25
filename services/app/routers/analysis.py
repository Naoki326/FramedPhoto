"""
analysis.py — 照片分析结果查询（AI 评分 / 每日精选元数据）。

管理台照片库 tab 化（#25）：/scores 支持按分类过滤 + 返回分类汇总
（名称 + 照片数，供 tab 排序与空分类合并显示）；未分类单列。
"""
from fastapi import APIRouter, HTTPException, Query

from app import category, db, daily
from app.config import settings

router = APIRouter()

UNCATEGORIZED = category.UNCATEGORIZED


def _sort_shot_at(rows: list[dict]) -> list[dict]:
    """按拍摄时间降序（缺失时间排后）。"""
    return sorted(rows, key=lambda r: (r.get("shot_at") is None, r.get("shot_at") or 0),
                  reverse=True)


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
    """照片缩略图（管理台照片库网格用）。仅允许 photo_lib_dir / uploads 内文件。"""
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
        img = Image.open(p)
        img.thumbnail((300, 400))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=80)
        return Response(content=buf.getvalue(), media_type="image/jpeg")
    except Exception:
        raise HTTPException(422, "bad image")
