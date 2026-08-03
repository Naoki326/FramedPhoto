"""
sync.py — NAS 同步状态查询（sync_nas.sh 写入 services/sync_status.json）。
"""
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()

_STATUS_FILE = Path(__file__).resolve().parent.parent.parent / "sync_status.json"
_STALE_AFTER_S = 120  # 超过此秒数无更新视为失联


@router.get("/status")
async def sync_status():
    """NAS 同步进度（status: running/done/error/idle）。"""
    if not _STATUS_FILE.exists():
        return {
            "status": "idle",
            "message": "尚未运行过同步",
            "command": "cd ~/FramedPhoto && ./scripts/sync_nas.sh",
        }
    try:
        data = json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"status": "error", "message": "状态文件损坏"}

    # 同步进程失联检测
    if data.get("status") == "running" and time.time() - data.get("updated_at", 0) > _STALE_AFTER_S:
        data["status"] = "stale"
        data["message"] = "同步进程失联（可能已退出，但未正常收尾）"
    return data


@router.post("/reset")
async def sync_status_reset():
    """清除同步状态（下次同步重新计数）。"""
    try:
        _STATUS_FILE.unlink()
    except OSError:
        pass
    return {"ok": True}
