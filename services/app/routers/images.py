"""
images.py — 图片上传 / 转换 / 内容管理（SQLite 持久化）。

- POST /upload       上传原始图片，转换并保存 FPS6 设备格式
- GET  ""            图片列表（Web 管理用）
- GET  /{id}/raw     FPS6 二进制（设备下载）
- GET  /{id}/preview PNG 预览（Web 展示）
- DELETE /{id}       删除图片
- GET  /content      内容清单（设备轮询，取最新一张）
"""
import hashlib
import json
import logging
import struct
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import Response

from app import db, daily
from app.config import settings
from app.epd_image import (
    HEADER_SIZE,
    SPECTRA6_PALETTE,
    PreparedImage,
    is_landscape,
    prepare_image,
    preview_png,
    preview_png_landscape,
)

logger = logging.getLogger(__name__)

router = APIRouter()

UPLOAD_DIR = Path(settings.upload_dir)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/upload")
async def upload(file: UploadFile = File(...), dither: bool = True, bg: BackgroundTasks = None):
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
                    prepared.width, prepared.height,
                    landscape=1 if is_landscape(raw) else 0)
    # 上传图片自动进入照片库（photo_scores）：异步 AI/启发式分析，可设为今日精选
    bg.add_task(_auto_analyze_upload, original_path, file.filename or "")
    meta = db.get_image(img_id)
    meta["preview_url"] = f"/api/images/{img_id}/preview"
    meta["raw_url"] = f"/api/images/{img_id}/raw"
    return meta


def _auto_analyze_upload(path: Path, filename: str) -> None:
    try:
        from app.analyzer import analyze_image
        from app import db as _db
        a = analyze_image(str(path))
        _db.upsert_photo_score(
            a.path, filename=a.filename or filename, caption=a.caption,
            description=a.description, type=a.type,
            memory_score=a.memory_score, beauty_score=a.beauty_score,
            reason=a.reason, shot_at=a.shot_at, shot_source=a.shot_source,
            gps_lat=a.gps_lat, gps_lon=a.gps_lon,
            source=a.source, analyzed_at=_db.now(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto analyze upload failed: %s", exc)


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


@router.get("/daily/raw")
async def get_daily_raw():
    """每日精选 FPS6（优先于手动上传内容时设备拉取）。"""
    if not daily.daily_fps6_exists():
        daily.render_daily()
    if not daily.daily_fps6_exists():
        raise HTTPException(404, "no daily photo")
    return Response(content=daily.DAILY_FILE.read_bytes(), media_type="application/octet-stream")


@router.get("/daily/selected")
async def daily_selected():
    """当前手动指定的今日照片（无则 null）。"""
    from app.runtime_config import load
    m = load().get("daily_manual") or {}
    return {"path": m.get("path", ""), "date": m.get("date", "")}


@router.post("/daily/select")
async def daily_select(body: dict):
    """手动指定今日精选照片（当日有效，次日恢复自动）。"""
    from datetime import datetime as _dt
    from pathlib import Path as _P
    from app import runtime_config
    from app.config import settings as _s
    path = (body.get("path") or "").strip()
    root = _P(_s.photo_lib_dir).resolve()
    p = _P(path).resolve()
    if not str(p).startswith(str(root)) or not p.is_file():
        raise HTTPException(404, "photo not found")
    runtime_config.save({"daily_manual": {"path": str(p), "date": _dt.now().strftime("%Y-%m-%d")}})
    return {"ok": True, "path": str(p)}


@router.delete("/daily/select")
async def daily_unselect():
    """取消手动指定，恢复自动选片。"""
    from app import runtime_config
    runtime_config.save({"daily_manual": {}})
    return {"ok": True}


@router.get("/daily/preview")
async def get_daily_preview():
    meta = daily.ensure_daily()
    if not meta:
        raise HTTPException(404, "no daily photo")
    raw = daily.DAILY_FILE.read_bytes()
    w, h = struct.unpack_from("<II", raw, 4)
    prepared = PreparedImage(width=w, height=h, data=raw, palette=SPECTRA6_PALETTE)
    return Response(content=preview_png_landscape(prepared, bool(meta.get("landscape"))),
                    media_type="image/png")


@router.get("/news/raw")
async def get_news_raw():
    """兼容旧地址：转发到自由模块（原新闻卡片）。"""
    return await get_free_raw()


def _free_module(module: str | None) -> str | None:
    """自由模块渲染的固定模块：优先 URL 参数；否则取当前时段的固定模块
    （与 /content 一致，保证 raw/preview 渲染的图和 content 返回的一致）。"""
    if module:
        return module
    from app import slots
    seg = slots.current_segment()
    if seg and seg.get("type") == slots.SLOT_FREE:
        return seg.get("module")
    return None


@router.get("/free/raw")
async def get_free_raw(refresh: bool = False, module: str | None = None):
    from app.free_module import render_free_fps6
    data = render_free_fps6(refresh=refresh, module_name=_free_module(module))
    if not data:
        raise HTTPException(503, "free module unavailable")
    return Response(content=data, media_type="application/octet-stream")


@router.get("/news/preview")
async def get_news_preview():
    """兼容旧地址：转发到自由模块预览。"""
    return await get_free_preview()


@router.get("/free/preview")
async def get_free_preview(module: str | None = None):
    from app.free_module import render_free_fps6
    data = render_free_fps6(module_name=_free_module(module))
    if not data:
        raise HTTPException(503, "free module unavailable")
    w, h = struct.unpack_from("<II", data, 4)
    prepared = PreparedImage(width=w, height=h, data=data, palette=SPECTRA6_PALETTE)
    return Response(content=preview_png_landscape(prepared, True), media_type="image/png")


@router.get("/weather/raw")
async def get_weather_raw():
    from app.weather_card import render_weather_card
    data = render_weather_card()
    if not data:
        raise HTTPException(503, "weather unavailable")
    return Response(content=data, media_type="application/octet-stream")


@router.get("/weather/preview")
async def get_weather_preview():
    from app.weather_card import render_weather_card
    data = render_weather_card()
    if not data:
        raise HTTPException(503, "weather unavailable")
    w, h = struct.unpack_from("<II", data, 4)
    prepared = PreparedImage(width=w, height=h, data=data, palette=SPECTRA6_PALETTE)
    return Response(content=preview_png_landscape(prepared, True), media_type="image/png")


# ---------- 临时显示（一次性上传切换，不保存到照片库 / 不入库分析） ----------

DISPLAY_FILE = UPLOAD_DIR / "display.fps6"
DISPLAY_META = UPLOAD_DIR / "display.json"


def _display_meta() -> dict | None:
    """临时显示图元数据（含内容指纹）；无则 None。"""
    if not DISPLAY_FILE.exists():
        return None
    m = {}
    if DISPLAY_META.exists():
        try:
            m = json.loads(DISPLAY_META.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            m = {}
    raw = DISPLAY_FILE.read_bytes()
    w, h = struct.unpack_from("<II", raw, 4)
    content_id = "display-" + hashlib.sha256(raw).hexdigest()[:10]
    return {
        "id": content_id,
        "filename": m.get("filename", ""),
        "landscape": 1 if m.get("landscape") else 0,
        "width": w,
        "height": h,
        "url": "/api/images/display/raw",
        "preview_url": "/api/images/display/preview",
    }


@router.post("/display/upload")
async def display_upload(file: UploadFile = File(...)):
    """一次性上传并切换当前显示（不入照片库、不触发 AI 分析，可随时覆盖/清除）。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    try:
        prepared = prepare_image(raw, settings.epd_width, settings.epd_height)
    except Exception as exc:
        raise HTTPException(422, f"image conversion failed: {exc}") from exc
    DISPLAY_FILE.write_bytes(prepared.data)
    DISPLAY_META.write_text(json.dumps({
        "filename": file.filename or "",
        "landscape": 1 if is_landscape(raw) else 0,
        "ts": db.now(),
    }, ensure_ascii=False), encoding="utf-8")
    return _display_meta()


@router.get("/display/current")
async def display_current():
    """当前显示：手动临时图优先，否则为时段自动内容。"""
    item = _display_meta()
    if item:
        return {"displaying": "manual", "item": item}
    return {"displaying": "auto", "item": None}


@router.get("/display/raw")
async def display_raw():
    if not DISPLAY_FILE.exists():
        raise HTTPException(404, "no display image")
    return Response(content=DISPLAY_FILE.read_bytes(), media_type="application/octet-stream")


@router.get("/display/preview")
async def display_preview():
    if not DISPLAY_FILE.exists():
        raise HTTPException(404, "no display image")
    raw = DISPLAY_FILE.read_bytes()
    w, h = struct.unpack_from("<II", raw, 4)
    prepared = PreparedImage(width=w, height=h, data=raw, palette=SPECTRA6_PALETTE)
    meta = _display_meta()
    return Response(content=preview_png_landscape(prepared, bool(meta and meta["landscape"])),
                    media_type="image/png")


@router.delete("/display/current")
async def display_clear():
    """清除临时显示，恢复时段自动内容。"""
    for p in (DISPLAY_FILE, DISPLAY_META):
        if p.exists():
            p.unlink()
    return {"ok": True}




@router.get("/{img_id}/raw")
async def get_raw(img_id: str):
    if img_id.startswith("display-"):
        # 内容指纹型 display id：设备按 id 拼 URL 下载，转发到临时显示图
        if not DISPLAY_FILE.exists():
            raise HTTPException(404, "no display image")
        return Response(content=DISPLAY_FILE.read_bytes(),
                        media_type="application/octet-stream")
    if img_id.startswith("daily-"):
        # 内容指纹型 daily id：设备按 id 拼 URL 下载，转发到每日精选
        if not daily.daily_fps6_exists():
            daily.render_daily()
        if not daily.daily_fps6_exists():
            raise HTTPException(404, "no daily photo")
        return Response(content=daily.DAILY_FILE.read_bytes(),
                        media_type="application/octet-stream")
    if img_id.startswith("news-") or img_id.startswith("free-"):
        from app.free_module import render_free_fps6
        data = render_free_fps6(module_name=_free_module(None))
        if not data:
            raise HTTPException(404, "no free module card")
        return Response(content=data, media_type="application/octet-stream")
    if img_id.startswith("weather-"):
        from app.weather_card import render_weather_card
        data = render_weather_card()
        if not data:
            raise HTTPException(404, "no weather card")
        return Response(content=data, media_type="application/octet-stream")
    meta = _get_or_404(img_id)
    data = (UPLOAD_DIR / meta["fps6_path"]).read_bytes()
    return Response(content=data, media_type="application/octet-stream")


@router.get("/{img_id}/preview")
async def get_preview(img_id: str):
    meta = _get_or_404(img_id)
    raw = (UPLOAD_DIR / meta["fps6_path"]).read_bytes()
    w, h = struct.unpack_from("<II", raw, 4)
    prepared = PreparedImage(width=w, height=h, data=raw, palette=SPECTRA6_PALETTE)
    return Response(content=preview_png_landscape(prepared, bool(meta.get("landscape"))),
                    media_type="image/png")


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


def _content_pushed() -> dict | None:
    """当日有效的内容库推送图；无则 None。"""
    from app.runtime_config import load
    p = load().get("content_push") or {}
    if not p.get("id") or p.get("date") != datetime.now().strftime("%Y-%m-%d"):
        return None
    m = db.get_image(p["id"])
    if not m:
        return None
    return {
        "id": m["id"],
        "filename": m.get("filename", ""),
        "width": m["width"],
        "height": m["height"],
        "landscape": m.get("landscape") or 0,
        "created_at": m.get("created_at"),
        "url": f"/api/images/{m['id']}/raw",
        "preview_url": f"/api/images/{m['id']}/preview",
    }


@router.get("/content/pushed")
async def content_pushed():
    from app.runtime_config import load
    p = load().get("content_push") or {}
    return {"pushed": p}


@router.post("/content/push")
async def content_push(body: dict):
    """将内容库某张图推送到显示（当日有效，次日自动回落）。"""
    from app.runtime_config import save
    img_id = (body.get("id") or "").strip()
    if not img_id:
        raise HTTPException(400, "missing id")
    if not db.get_image(img_id):
        raise HTTPException(404, "image not found")
    save({"content_push": {"id": img_id, "date": datetime.now().strftime("%Y-%m-%d")}})
    return {"ok": True, "id": img_id}


@router.delete("/content/push")
async def content_unpush():
    from app.runtime_config import save
    save({"content_push": {}})
    return {"ok": True}


@router.get("/content")
async def content_list(device_id: str | None = None):
    """内容清单：设备轮询此接口。

    优先级：临时显示（display，一次性上传切换）> 时段编排（见 app.slots，
    slot_segments 分段，未配置时兼容旧 slot_weather / slot_photo / slot_free）：
      weather 时段 → 天气+日历卡片（QWEATHER_KEY 未配/失败时回退照片）
      photo    时段 → 每日精选照片
      free     时段 → 自由模块卡片（LLM+文生图每日生成；依赖未配置时回退照片）
    可附带设备 id 上报状态。
    """
    from app import slots
    from app.weather_card import render_weather_slot

    result = []
    source = ""

    # 临时显示（一次性上传切换）优先于一切时段内容
    item = _display_meta()
    if item:
        result = [item]
        source = "display"
    else:
        slot = slots.current_slot()
        source = slot

        if slot == slots.SLOT_WEATHER:
            meta = render_weather_slot()
            if meta:
                result.append({
                    "id": meta["id"],
                    "filename": meta.get("filename", ""),
                    "caption": meta.get("caption", ""),
                    "date": meta.get("date", ""),
                    "memory_score": None,
                    "width": meta["width"],
                    "height": meta["height"],
                    "landscape": 1,  # 天气卡片横屏
                    "url": "/api/images/weather/raw",
                    "preview_url": "/api/images/weather/preview",
                })
        elif slot == slots.SLOT_FREE:
            from app.free_module import render_free_slot
            seg = slots.current_segment()
            mod = (seg or {}).get("module")
            meta = render_free_slot(module_name=mod)
            if meta:
                qs = f"?module={quote(mod)}" if mod else ""
                result.append({
                    "id": meta["id"],
                    "filename": meta.get("filename", ""),
                    "caption": meta.get("caption", ""),
                    "date": meta.get("date", ""),
                    "memory_score": None,
                    "width": meta["width"],
                    "height": meta["height"],
                    "landscape": 1,  # 自由模块卡片横屏
                    "url": "/api/images/free/raw" + qs,
                    "preview_url": "/api/images/free/preview" + qs,
                })

        if not result:
            # 照片时段，或天气/新闻不可用时的回退。
            # 照片时段优先级：内容库「推送到显示」的图 > 手动指定今日精选 > 每日精选。
            # 内容库未推送的图只入库不上屏（须在内容库点「推送到显示」才上屏）。
            if slot == slots.SLOT_PHOTO:
                pushed = _content_pushed()
                if pushed:
                    result.append(pushed)
                    source = "content_push"
            if not result:
                meta = daily.ensure_daily()
                if meta:
                    source = "daily"
                    result.append({
                        "id": meta.get("id", "daily"),
                        "filename": meta.get("filename", ""),
                        "caption": meta.get("caption", ""),
                        "date": meta.get("date", ""),
                        "memory_score": meta.get("memory_score"),
                        "width": meta["width"],
                        "height": meta["height"],
                        "landscape": meta.get("landscape") or 0,
                        "url": "/api/images/daily/raw",
                        "preview_url": "/api/images/daily/preview",
                    })

    # 设备附带上报心跳（与 /heartbeat 等价，减少一次请求）
    if device_id:
        db.upsert_device(device_id, current_image=result[0]["id"] if result else "",
                         last_seen=db.now())
    return {"images": result, "source": source}
