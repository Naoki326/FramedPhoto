"""
analysis.py — 照片分析结果查询（AI 评分 / 每日精选元数据）。
"""
from fastapi import APIRouter, HTTPException, Query

from app import db, daily
from app.config import settings

router = APIRouter()


@router.get("/scores")
async def list_scores(limit: int = Query(50, ge=1, le=500)):
    """照片评分列表（回忆度降序）。"""
    rows = db.list_photo_scores(limit=limit)
    return {"scores": rows}


@router.get("/daily")
async def daily_meta():
    """今日精选元数据（无则 null）。"""
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
