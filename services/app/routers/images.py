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
import io
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageOps

from app import content_sources, db, daily, nas_sync
from app.config import settings
from app.epd_image import (
    is_landscape,
    parse_fps6,
    prepare_image,
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


def _sync_nas_job(img_id: str, filename: str) -> None:
    try:
        result = nas_sync.push_content_image(img_id, filename)
        db.update_image(
            img_id,
            nas_status="saved",
            nas_path=result["remote_path"],
            nas_sha256=result["sha256"],
            nas_synced_at=db.now(),
            nas_error="",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("sync content image to NAS failed: %s", img_id)
        db.update_image(img_id, nas_status="failed", nas_error=str(exc)[:500])


@router.post("/{img_id}/sync-nas")
async def sync_image_to_nas(img_id: str, bg: BackgroundTasks):
    """把内容库原图复制到 NAS 专用目录，并同步到本地 NAS 镜像。"""
    meta = _get_or_404(img_id)
    if not nas_sync.configured():
        raise HTTPException(503, "NAS 未配置 SSH 连接参数")
    original = UPLOAD_DIR / f"{img_id}.orig"
    if not original.is_file():
        raise HTTPException(409, "内容库原图不存在，无法同步到 NAS")
    if meta.get("nas_status") == "uploading":
        raise HTTPException(409, "这张图片正在同步到 NAS")
    if meta.get("nas_status") == "saved":
        return {"ok": True, "status": "saved", "nas_path": meta.get("nas_path", "")}
    db.update_image(img_id, nas_status="uploading", nas_error="")
    bg.add_task(_sync_nas_job, img_id, meta.get("filename", ""))
    return {"ok": True, "status": "uploading", "id": img_id}


@router.post("/{img_id}/delete-local")
async def delete_local_after_nas(img_id: str):
    """仅在 NAS 远端文件再次通过大小校验后，删除内容库本地副本。"""
    meta = _get_or_404(img_id)
    if meta.get("nas_status") != "saved" or not meta.get("nas_path"):
        raise HTTPException(409, "请先完成 NAS 同步并通过远端校验")
    original = UPLOAD_DIR / f"{img_id}.orig"
    if not original.is_file():
        raise HTTPException(409, "本地原图已经不存在")
    if not nas_sync.verify_remote_file(meta["nas_path"], original.stat().st_size):
        raise HTTPException(409, "NAS 文件校验失败，未删除本地文件")

    for suffix in ("orig", "fps6", "thumb.jpg"):
        path = UPLOAD_DIR / f"{img_id}.{suffix}"
        if path.exists():
            path.unlink()
    for path in (str(original), str(original.resolve())):
        db.delete_photo_score(path)
    from app import runtime_config
    if (runtime_config.load().get("content_push") or {}).get("id") == img_id:
        runtime_config.save({"content_push": {}})
    db.delete_image(img_id)
    return {"ok": True, "deleted": img_id, "nas_path": meta["nas_path"]}


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
    """手动指定今日精选照片（当日有效，次日恢复自动）。

    接受照片库（photo_lib_dir）与内容库上传图（upload_dir）下的路径；
    优先以 photo_scores 里记录的路径形式保存（上传图入库为相对路径
    如 uploads/xx.orig），确保 daily_manual_pick 能查到评分记录。
    """
    from datetime import datetime as _dt
    from pathlib import Path as _P
    from app import runtime_config
    from app.config import settings as _s
    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(404, "photo not found")
    p = _P(path).resolve()
    roots = [_P(_s.photo_lib_dir).resolve(), _P(_s.upload_dir).resolve()]
    if not any(p.is_relative_to(r) for r in roots) or not p.is_file():
        raise HTTPException(404, "photo not found")
    # 保存 DB 记录使用的路径形式（上传图为相对路径），保证手动指定可被查到
    db_path = path if db.get_photo_score(path) else str(p)
    runtime_config.save({"daily_manual": {"path": db_path, "date": _dt.now().strftime("%Y-%m-%d")}})
    return {"ok": True, "path": db_path}


@router.delete("/daily/select")
async def daily_unselect():
    """取消手动指定，恢复自动选片。"""
    from app import runtime_config
    runtime_config.save({"daily_manual": {}})
    return {"ok": True}


@router.get("/daily/preview")
async def get_daily_preview():
    """每日精选预览：原图缩略图（省流量）。预览永远由原图缩放
    而来（ADR-0001）：原图缺失/不可读 → 404，不做帧还原兜底；
    原图失效由次日选片的既有重选逻辑自然处理。
    """
    meta = daily.ensure_daily()
    if not meta:
        raise HTTPException(404, "no daily photo")
    path = meta.get("path")
    try:
        if path and Path(path).is_file():
            return Response(content=_make_thumbnail(Path(path).read_bytes()),
                            media_type="image/jpeg")
    except Exception:
        pass
    raise HTTPException(404, "daily photo original missing")


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
    """自由模块预览：返回最近保存的卡片原图缩略图（量化前，省流量）。

    自由模块渲染时 _save_to_library 会把加文字后的完整卡片 PNG 原图
    （插画与降级文字卡两条路径）存入 uploads/{id}.orig。按 mtime 找最新一张
    （即当前显示内容）。预览永远由原图缩放而来（ADR-0001）：
    无卡片原图 → 404，不做帧还原兑底。module 参数仅为维持端点形状
    保留（端点重排留给后续 ContentSource 改造，见 epic #1）。
    """
    try:
        up = Path(settings.upload_dir)
        origs = sorted(up.glob("*.orig"), key=lambda p: p.stat().st_mtime, reverse=True)
        if origs:
            return Response(content=_make_thumbnail(origs[0].read_bytes(), max_side=800),
                            media_type="image/jpeg")
    except Exception:
        pass
    raise HTTPException(404, "free card original missing")


@router.get("/weather/raw")
async def get_weather_raw():
    from app.weather_card import render_weather_card
    data = render_weather_card()
    if not data:
        raise HTTPException(503, "weather unavailable")
    return Response(content=data, media_type="application/octet-stream")


@router.get("/weather/preview")
async def get_weather_preview():
    """天气卡片预览：先按既有缓存节奏渲染，再读同 stem 原图返回缩略。

    预览永远由原图缩放而来（ADR-0001）：渲染不可用 → 503；
    原图缺失（如旧缓存无原图）→ 404，不做帧还原兜底。
    """
    from app import weather_card
    if not weather_card.render_weather_card():
        raise HTTPException(503, "weather unavailable")
    png = weather_card.weather_original_png()
    if not png:
        raise HTTPException(404, "weather original missing")
    return Response(content=_make_thumbnail(png, max_side=800), media_type="image/jpeg")


# ---------- 临时显示（一次性上传切换，不保存到照片库 / 不入库分析） ----------

DISPLAY_FILE = UPLOAD_DIR / "display.fps6"
DISPLAY_META = UPLOAD_DIR / "display.json"
# 原始图缩略图（量化前，供管理台「当前显示」预览，省流量）
DISPLAY_ORIG = UPLOAD_DIR / "display.orig.jpg"


def _display_meta() -> dict | None:
    """临时显示图元数据（含内容指纹）；无则 None。

    帧头解读统一走 parse_fps6（内部自检口径，不校验尺寸）：
    DISPLAY_FILE 损坏不可解析时视同无临时显示（回落时段自动内容）。
    """
    if not DISPLAY_FILE.exists():
        return None
    m = {}
    if DISPLAY_META.exists():
        try:
            m = json.loads(DISPLAY_META.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            m = {}
    raw = DISPLAY_FILE.read_bytes()
    try:
        prepared = parse_fps6(raw)
    except ValueError as exc:
        logger.warning("display frame unreadable, treating as no manual display: %s", exc)
        return None
    content_id = "display-" + hashlib.sha256(raw).hexdigest()[:10]
    return {
        "id": content_id,
        "filename": m.get("filename", ""),
        "landscape": 1 if m.get("landscape") else 0,
        "width": prepared.width,
        "height": prepared.height,
        "url": "/api/images/display/raw",
        "preview_url": "/api/images/display/preview",
        "has_orig": bool(m.get("has_orig")),
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
    # 保存量化前原图缩略图（管理台「当前显示」预览用，省流量）
    _write_display_orig(raw)
    DISPLAY_META.write_text(json.dumps({
        "filename": file.filename or "",
        "landscape": 1 if is_landscape(raw) else 0,
        "has_orig": True,
        "ts": db.now(),
    }, ensure_ascii=False), encoding="utf-8")
    return _display_meta()


def _write_display_orig(raw: bytes) -> None:
    """把原始图片转成缩略图保存（最长边 ~500px，JPEG 80），失败时静默。"""
    try:
        DISPLAY_ORIG.write_bytes(_make_thumbnail(raw))
    except Exception:
        try:
            DISPLAY_ORIG.unlink(missing_ok=True)
        except OSError:
            pass


def _make_thumbnail(raw: bytes, max_side: int = 500, quality: int = 80) -> bytes:
    """原始图片 -> JPEG 缩略图字节（量化前原图，省流量）。"""
    import io
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _classify_fps6_error(msg: str) -> tuple[str, tuple[int, int] | None]:
    """把 parse_fps6 的失败消息分类，供直推/校准端点回各自既有的 400 文案。

    返回 ("magic"， None)（magic 错或头不全）| ("size", 实际宽高) |
    ("length", None)（截断或长度不匹配）。
    """
    if "magic" in msg or "header" in msg:
        return "magic", None
    m = re.search(r"size (\d+)x(\d+) != expected", msg)
    if m:
        return "size", (int(m.group(1)), int(m.group(2)))
    return "length", None


@router.post("/display/raw-fps6")
async def display_upload_raw_fps6(file: UploadFile = File(...)):
    """校准用：接收已量化的 FPS6 文件直接显示，不重新量化。

    用于 tools/generate_calibration_chart.py 生成的校准图——该图以设备
    nibble 直写六种纯色，必须绕过服务端量化，否则纯色会被重算破坏。
    校验统一走 parse_fps6（magic/尺寸/长度），保留既有 400 语义与消息；
    校验通过后原样写入 DISPLAY_FILE，设备刷新即显示。
    """
    raw = await file.read()
    try:
        parse_fps6(raw, expected_size=(settings.epd_width, settings.epd_height))
    except ValueError as exc:
        kind, actual = _classify_fps6_error(str(exc))
        if kind == "magic":
            raise HTTPException(400, "not a FPS6 file") from exc
        if kind == "size":
            raise HTTPException(
                400, f"size must be {settings.epd_width}x{settings.epd_height},"
                     f" got {actual[0]}x{actual[1]}") from exc
        raise HTTPException(400, "truncated FPS6 payload") from exc
    DISPLAY_FILE.write_bytes(raw)
    DISPLAY_ORIG.unlink(missing_ok=True)   # 校准图直推无原图，清理旧缩略图
    DISPLAY_META.write_text(json.dumps({
        "filename": file.filename or "calibration.fps6",
        "landscape": 1,   # 校准图按横放视角设计，设备横放观看
        "has_orig": False,
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
    """当前显示预览：有原图缩略图则返回原图（量化前，省流量）。

    预览永远由原图而来（ADR-0001）：无原图（如直推的外部 FPS6 /
    校准图，本就无原图）→ 404，不做帧还原兜底。
    """
    if not DISPLAY_FILE.exists():
        raise HTTPException(404, "no display image")
    if DISPLAY_ORIG.exists():
        return Response(content=DISPLAY_ORIG.read_bytes(), media_type="image/jpeg")
    raise HTTPException(404, "display original missing")


@router.delete("/display/current")
async def display_clear():
    """清除临时显示，恢复时段自动内容。"""
    for p in (DISPLAY_FILE, DISPLAY_META, DISPLAY_ORIG):
        if p.exists():
            p.unlink()
    return {"ok": True}




@router.get("/{img_id}/raw")
async def get_raw(img_id: str):
    if img_id.startswith("display-"):
        # 内容指纹型 display id：设备按 id 拼 URL 下载，转发到临时显示图
        # （display 源适配器待后续改造搬入注册表，双轨期保持原分支）
        if not DISPLAY_FILE.exists():
            raise HTTPException(404, "no display image")
        return Response(content=DISPLAY_FILE.read_bytes(),
                        media_type="application/octet-stream")
    # 内容源注册表分发（ADR-0002）：daily-/weather-/free-/news- 前缀 → 源，
    # 源服务「当前内容」（id 仅是内容指纹，不作查找键）
    source = content_sources.resolve(img_id)
    if source:
        data = source.render(img_id)
        if not data:
            raise HTTPException(404, source.missing_detail)
        return Response(content=data, media_type="application/octet-stream")
    meta = _get_or_404(img_id)
    data = (UPLOAD_DIR / meta["fps6_path"]).read_bytes()
    return Response(content=data, media_type="application/octet-stream")


@router.get("/{img_id}/preview")
async def get_preview(img_id: str):
    """内容库图片预览：返回原图原样字节（探测 PNG/JPEG 定 Content-Type）。
    预览永远由原图而来（ADR-0001）：原图缺失 → 404，不做帧还原兜底。
    """
    _get_or_404(img_id)
    orig = UPLOAD_DIR / f"{img_id}.orig"
    if orig.exists():
        head = orig.read_bytes()[:12]
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return Response(content=orig.read_bytes(), media_type="image/png")
        if head[:3] == b"\xff\xd8\xff":
            return Response(content=orig.read_bytes(), media_type="image/jpeg")
    raise HTTPException(404, "image original missing")


THUMB_WIDTH = 400  # 内容库缩略图宽度（省流量）


@router.get("/{img_id}/thumb")
async def get_thumb(img_id: str):
    """内容库缩略图：原图缩放为 ~400px 宽 JPEG，磁盘缓存，省流量。
    缩略缓存或原图可用 → 200；均不可用 → 404，不做帧还原兜底（ADR-0001）。
    """
    _get_or_404(img_id)
    thumb = UPLOAD_DIR / f"{img_id}.thumb.jpg"
    if thumb.exists():
        return Response(content=thumb.read_bytes(), media_type="image/jpeg")
    orig = UPLOAD_DIR / f"{img_id}.orig"
    if not orig.exists():
        raise HTTPException(404, "image original missing")
    img = Image.open(io.BytesIO(orig.read_bytes()))
    img = img.convert("RGB")
    w, h = img.size
    if w > THUMB_WIDTH:
        img = img.resize((THUMB_WIDTH, max(1, round(h * THUMB_WIDTH / w))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    data = buf.getvalue()
    try:
        thumb.write_bytes(data)
    except OSError:
        pass
    return Response(content=data, media_type="image/jpeg")


@router.get("/{img_id}/download")
async def download_orig(img_id: str):
    """下载原图（.orig 全彩文件，带文件名）。"""
    meta = _get_or_404(img_id)
    orig = UPLOAD_DIR / f"{img_id}.orig"
    if not orig.exists():
        raise HTTPException(404, "no original file")
    head = orig.read_bytes()[:12]
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        ct = "image/png"
    elif head[:3] == b"\xff\xd8\xff":
        ct = "image/jpeg"
    else:
        ct = "application/octet-stream"
    fname = (meta.get("filename") or img_id).strip() or img_id
    fname_ascii = re.sub(r"[^A-Za-z0-9._\-]", "_", fname)
    cd = f"attachment; filename=\"{fname_ascii}\"; filename*=UTF-8''{quote(fname)}"
    return Response(content=orig.read_bytes(), media_type=ct,
                    headers={"Content-Disposition": cd})


@router.delete("/{img_id}")
async def delete_image(img_id: str):
    meta = _get_or_404(img_id)
    # 删除磁盘文件（尽力而为）
    for suffix in ("orig", "fps6", "thumb.jpg"):
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
