"""
images.py — 图片上传 / 转换 / 内容管理（SQLite 持久化）。

- POST /upload       上传原始图片，转换并保存 FPS6 设备格式
- GET  ""            图片列表（Web 管理用）
- GET  /{id}/raw     FPS6 二进制（设备下载）
- GET  /{id}/preview PNG 预览（Web 展示）
- DELETE /{id}       删除图片
- GET  /content      内容清单（设备轮询，取最新一张）
"""
import struct
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from app import db
from app.config import settings
from app.epd_image import HEADER_SIZE, SPECTRA6_PALETTE, PreparedImage, prepare_image, preview_png

router = APIRouter()

UPLOAD_DIR = Path(settings.upload_dir)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/upload")
async def upload(file: UploadFile = File(...), dither: bool = True):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")

    img_id = uuid.uuid4().hex[:12]
    try:
        prepared = prepare_image(raw, settings.epd_width, settings.epd_height, dither=dither)
    except Exception as exc:
        raise HTTPException(422, f"image conversion failed: {exc}") from exc

    original_path = UPLOAD_DIR / f"{img_id}.orig"
    fps6_path = UPLOAD_DIR / f"{img_id}.fps6"
    original_path.write_bytes(raw)
    fps6_path.write_bytes(prepared.data)

    db.insert_image(img_id, file.filename or "", fps6_path.name,
                    prepared.width, prepared.height)
    meta = db.get_image(img_id)
    meta["preview_url"] = f"/api/images/{img_id}/preview"
    meta["raw_url"] = f"/api/images/{img_id}/raw"
    return meta


@router.get("")
async def list_images():
    items = db.list_images()
    for m in items:
        m["preview_url"] = f"/api/images/{m['id']}/preview"
        m["raw_url"] = f"/api/images/{m['id']}/raw"
    return {"images": items}


def _get_or_404(img_id: str) -> dict:
    meta = db.get_image(img_id)
    if not meta:
        raise HTTPException(404, "image not found")
    return meta


@router.get("/{img_id}/raw")
async def get_raw(img_id: str):
    meta = _get_or_404(img_id)
    data = (UPLOAD_DIR / meta["fps6_path"]).read_bytes()
    return Response(content=data, media_type="application/octet-stream")


@router.get("/{img_id}/preview")
async def get_preview(img_id: str):
    meta = _get_or_404(img_id)
    raw = (UPLOAD_DIR / meta["fps6_path"]).read_bytes()
    w, h = struct.unpack_from("<II", raw, 4)
    prepared = PreparedImage(width=w, height=h, data=raw, palette=SPECTRA6_PALETTE)
    return Response(content=preview_png(prepared), media_type="image/png")


@router.delete("/{img_id}")
async def delete_image(img_id: str):
    meta = _get_or_404(img_id)
    # 删除磁盘文件（尽力而为）
    for suffix in ("orig", "fps6"):
        p = UPLOAD_DIR / f"{img_id}.{suffix}"
        if p.exists():
            p.unlink()
    db.delete_image(img_id)
    return {"ok": True, "deleted": img_id}


@router.get("/content")
async def content_list(device_id: str | None = None):
    """内容清单：设备轮询此接口，取最新图片；可附带设备 id 上报状态。"""
    items = db.list_images()
    result = []
    for m in items[:10]:  # 最多返回 10 张，设备默认取第一张
        result.append({
            "id": m["id"],
            "filename": m["filename"],
            "width": m["width"],
            "height": m["height"],
            "created_at": m["created_at"],
            "url": f"/api/images/{m['id']}/raw",
        })
    # 设备附带上报心跳（与 /heartbeat 等价，减少一次请求）
    if device_id:
        db.upsert_device(device_id, current_image=result[0]["id"] if result else "", last_seen=db.now())
    return {"images": result}
