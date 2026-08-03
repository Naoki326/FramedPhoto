"""
ota.py — 固件发布与设备升级清单。

- POST /upload    上传固件 bin（计算 sha256，入库，成为最新版本）
- GET  /manifest  设备轮询：返回最新固件信息（无新固件时 detail=null）
- GET  /download  下载固件 bin
- GET  /versions  固件版本历史
"""
import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app import db
from app.config import settings

router = APIRouter()

OTA_DIR = Path(settings.ota_dir)
OTA_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/upload")
async def upload_firmware(file: UploadFile = File(...), version: str = Form("")):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if not raw.startswith(b"\xe9"):
        # 0xE9 是 ESP-IDF app 镜像的魔数（0xE9 头部）
        raise HTTPException(422, "not an ESP-IDF firmware image (magic 0xE9)")

    ver = (version or "").strip()
    if not ver:
        # 用时间戳作为版本号兜底
        ver = f"build-{uuid.uuid4().hex[:8]}"

    sha = hashlib.sha256(raw).hexdigest()
    file_name = f"framedphoto-{ver}.bin"
    file_path = OTA_DIR / file_name
    file_path.write_bytes(raw)

    db.upsert_firmware(ver, file_name, str(file_path), sha, len(raw))
    return {
        "version": ver,
        "sha256": sha,
        "size": len(raw),
        "download_url": "/api/ota/download",
    }


@router.get("/manifest")
async def get_manifest(device_id: str | None = None, current_version: str = ""):
    """设备轮询：返回可升级固件；设备已是最新时 detail=null。"""
    fw = db.get_latest_firmware()
    if not fw:
        return {"detail": None, "reason": "no firmware published"}
    if fw["version"] == current_version:
        return {"detail": None, "reason": "already latest"}
    return {
        "detail": {
            "version": fw["version"],
            "size": fw["size"],
            "sha256": fw["sha256"],
            "url": "/api/ota/download",
        }
    }


@router.get("/download")
async def download():
    fw = db.get_latest_firmware()
    if not fw:
        raise HTTPException(404, "no firmware")
    path = Path(fw["file_path"])
    if not path.exists():
        raise HTTPException(404, "firmware file missing")
    return FileResponse(path, media_type="application/octet-stream",
                        filename=fw["filename"])


@router.get("/versions")
async def versions():
    return {"firmware": db.get_latest_firmware()}
