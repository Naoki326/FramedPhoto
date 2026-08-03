"""
analysis.py — 照片分析结果查询（AI 评分 / 每日精选元数据）。
"""
from fastapi import APIRouter, Query

from app import db, daily

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
