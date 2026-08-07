"""
sync.py — NAS 同步状态查询（sync_nas.sh 写入 services/sync_status.json）。
"""
import json
import os
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

_SERVICES_DIR = Path(__file__).resolve().parents[2]
_REPO_DIR = _SERVICES_DIR.parent
_STATUS_FILE = _SERVICES_DIR / "sync_status.json"
_SYNC_SCRIPT = _REPO_DIR / "scripts" / "sync_nas.sh"
_SYNC_LOG = _SERVICES_DIR / "sync_nas.log"
_STALE_AFTER_S = 120  # 超过此秒数无更新视为失联


def _read_status() -> dict:
    if not _STATUS_FILE.exists():
        return {"status": "idle"}
    try:
        return json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"status": "error", "message": "状态文件损坏"}


@router.post("/start")
async def sync_start():
    """从管理台手动启动一次 NAS -> 本地的增量同步。"""
    status = _read_status()
    if status.get("status") == "running":
        updated = float(status.get("updated_at", 0) or 0)
        if time.time() - updated <= _STALE_AFTER_S:
            raise HTTPException(409, "NAS 同步正在进行中")
    if not _SYNC_SCRIPT.is_file():
        raise HTTPException(500, "同步脚本不存在")
    try:
        with _SYNC_LOG.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(_SYNC_SCRIPT)],
                cwd=str(_REPO_DIR),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        raise HTTPException(500, f"无法启动 NAS 同步: {exc}") from exc
    return {"ok": True, "status": "started", "pid": process.pid}


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
