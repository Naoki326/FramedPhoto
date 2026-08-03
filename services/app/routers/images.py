"""
images.py — 图片上传 / 转换 / 内容清单。

- POST /upload      上传原始图片，返回转换后的 FramedPhoto 索引图
- GET  /{id}/raw    取索引图二进制（设备下载用）
- GET  /{id}/preview 取 PNG 预览（Web 展示用）
- GET  /content     内容清单：设备轮询当前应显示的图片列表
"""
import uuid
import struct
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from app.config import settings
from app.epd_image import prepare_image, preview_png

router = APIRouter()

UPLOAD_DIR = Path(settings.upload_dir)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_meta: dict[str, dict] = {}  # id -> {original, fps6, w, h, created_at}


@router.post("/upload")
async def upload(file: UploadFile = File(...), dither: bool = True):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")

    img_id = uuid.uuid4().hex[:12]
    try:
        prepared = prepare_image(raw, settings.epd_width, settings.epd_height, dither=dither)
    except Exception as exc:  # Pillow 解码失败等
        raise HTTPException(422, f"image conversion failed: {exc}") from exc

    original_path = UPLOAD_DIR / f"{img_id}.orig"
    fps6_path = UPLOAD_DIR / f"{img_id}.fps6"
    original_path.write_bytes(raw)
    fps6_path.write_bytes(prepared.data)

    _meta[img_id] = {
        "id": img_id,
        "original": original_path.name,
        "fps6": fps6_path.name,
        "width": prepared.width,
        "height": prepared.height,
        "name": file.filename,
    }
    return _meta[img_id]


@router.get("/{img_id}/raw")
async def get_raw(img_id: str):
    if img_id not in _meta:
        raise HTTPException(404, "not found")
    data = (UPLOAD_DIR / _meta[img_id]["fps6"]).read_bytes()
    return Response(content=data, media_type="application/octet-stream")


@router.get("/{img_id}/preview")
async def get_preview(img_id: str):
    if img_id not in _meta:
        raise HTTPException(404, "not found")
    from app.epd_image import HEADER_SIZE, SPECTRA6_PALETTE, PreparedImage, preview_png

    raw = (UPLOAD_DIR / _meta[img_id]["fps6"]).read_bytes()
    w, h = struct.unpack_from("<II", raw, 4)
    prepared = PreparedImage(width=w, height=h, data=raw, palette=SPECTRA6_PALETTE)
    return Response(content=preview_png(prepared), media_type="image/png")


@router.get("/content")
async def content_list():
    """内容清单：按创建时间倒序返回全部图片（M3 增加设备定向与调度）。"""
    items = sorted(_meta.values(), key=lambda m: m.get("created_at") or "", reverse=True)
    return {"images": items}
