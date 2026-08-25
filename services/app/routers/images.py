"""
images.py — 图片上传 / 转换 / 内容管理（SQLite 持久化）。

- POST /upload       上传原始图片，转换并保存 FPS6 设备格式
- GET  ""            图片列表（Web 管理用）
- GET  /{id}/raw     FPS6 二进制——唯一设备下载契约（五源经内容源注册表
                     分发，ADR-0002；旧固定 raw/preview 路由已退役
                     （T7 #17），news- 前缀的旧固件兜底保留在前缀分发）
- GET  /{id}/preview 内容预览（五源共用：原图缩放 ~800px JPEG，缺原图 404）
- DELETE /{id}       删除图片
- GET  /content      内容清单（设备轮询，取最新一张）
"""
import io
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image

from app import (
    category,
    content_sources,
    daily,
    db,
    display,
    free_module,
    nas_sync,
    runtime_config,
    slots,
    weather_card,
)
from app.analyzer import analyze_image
from app.config import settings
from app.epd_image import (
    is_landscape,
    make_thumbnail,
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
        a = analyze_image(str(path))
        # 内容哈希迁移（ADR-0005）：内容库上传图也走同一识别逻辑，
        # 未被分类（归未分类）；复制/移动语义与照片库一致。
        category.migrate_or_record(a)
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
    if (runtime_config.get("content_push") or {}).get("id") == img_id:
        runtime_config.save({"content_push": {}})
    db.delete_image(img_id)
    return {"ok": True, "deleted": img_id, "nas_path": meta["nas_path"]}


@router.get("/daily/selected")
async def daily_selected():
    """当前手动指定的今日照片（无则 null）。"""
    m = runtime_config.get("daily_manual") or {}
    return {"path": m.get("path", ""), "date": m.get("date", "")}


@router.post("/daily/select")
async def daily_select(body: dict):
    """手动指定今日精选照片（当日有效，次日恢复自动）。

    接受照片库（photo_lib_dir）与内容库上传图（upload_dir）下的路径；
    优先以 photo_scores 里记录的路径形式保存（上传图入库为相对路径
    如 uploads/xx.orig），确保 daily_manual_pick 能查到评分记录。
    """
    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(404, "photo not found")
    p = Path(path).resolve()
    roots = [Path(settings.photo_lib_dir).resolve(), Path(settings.upload_dir).resolve()]
    if not any(p.is_relative_to(r) for r in roots) or not p.is_file():
        raise HTTPException(404, "photo not found")
    # 保存 DB 记录使用的路径形式（上传图为相对路径），保证手动指定可被查到
    db_path = path if db.get_photo_score(path) else str(p)
    runtime_config.save(
        {"daily_manual": {"path": db_path, "date": datetime.now().strftime("%Y-%m-%d")}})
    return {"ok": True, "path": db_path}


@router.delete("/daily/select")
async def daily_unselect():
    """取消手动指定，恢复自动选片。"""
    runtime_config.save({"daily_manual": {}})
    return {"ok": True}


def _free_module(module: str | None) -> str | None:
    """自由模块渲染的固定模块：优先 URL 参数；否则取当前时段的固定模块
    （与 /content 一致，保证 raw/preview 渲染的图和 content 返回的一致）。"""
    if module:
        return module
    seg = slots.current_segment()
    if seg and seg.get("type") == slots.SLOT_FREE:
        return seg.get("module")
    return None


@router.post("/free/regenerate")
async def free_regenerate():
    """重新生成今日自由模块（管理台重生成按钮，ADR-0002）。

    与设备下载同一条生成路径：render_free_fps6(refresh=True) 跳过缓存
    强制重生成，选模块与 /content 一致（经 _free_module）；成功返回新
    卡片的清单条目（url/preview_url 由统一 helper _item_urls 组装为
    /api/images/{id}/raw|preview 形态），自由模块不可用 → 503（沿用固定
    路由时代 refresh 参数的失败语义；T7 #17 后本端点是唯一重生成入口）。"""
    data = free_module.render_free_fps6(refresh=True, module_name=_free_module(None))
    if not data:
        raise HTTPException(503, "free module unavailable")
    item = free_module.SOURCE.meta()
    return {"ok": True, "item": _item_urls(item) if item else None}


# ---------- 临时显示（一次性上传切换，不保存到照片库 / 不入库分析） ----------
# 文件协议（帧 / 元数据 json / 原图缩略图与内容指纹 id）在领域模块
# app/display.py（内容源适配器同步注册进 content_sources，ADR-0002）


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
    return display.save_upload(raw, prepared, file.filename or "")


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
    校验通过后由 display 领域模块原样落帧，设备刷新即显示。
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
    return display.save_raw_fps6(raw, file.filename or "calibration.fps6")


@router.get("/display/current")
async def display_current():
    """当前显示：手动临时图优先，否则为时段自动内容。

    消费置顶显示源的元数据方法（meta()，ADR-0002），url/preview_url 由
    模块级 helper _item_urls 统一组装为 /api/images/{id}/raw|preview 形态。"""
    meta = display.SOURCE.meta()
    if meta:
        return {"displaying": "manual", "item": _item_urls(meta)}
    return {"displaying": "auto", "item": None}


@router.delete("/display/current")
async def display_clear():
    """清除临时显示，恢复时段自动内容。"""
    display.clear()
    return {"ok": True}




@router.get("/{img_id}/raw")
async def get_raw(img_id: str):
    # 内容源注册表分发（ADR-0002）：五种内容源全部经注册表分发——
    # display-/daily-/weather-/free-/news- 前缀各归其源，无前缀兑底
    # 内容库（id 是查找键）。源服务「当前内容」，路由层不感知具体类型。
    source = content_sources.resolve(img_id)
    data = source.render(img_id)
    if not data:
        raise HTTPException(404, source.missing_detail)
    return Response(content=data, media_type="application/octet-stream")


@router.get("/{img_id}/preview")
async def get_preview(img_id: str):
    """内容预览（五源共用，T4 #14）：原图缩放 ~800px JPEG。

    与 /{id}/raw 同经内容源注册表分发（ADR-0002）：五种内容源的 id
    均可预览，统一由原图缩放而来（ADR-0001）；缺原图一律 404，
    不做帧还原兑底。内容库的全尺寸原图由既有下载端点 /{id}/download 承接。
    """
    source = content_sources.resolve(img_id)
    orig = source.original(img_id)
    if not orig:
        raise HTTPException(404, "preview original missing")
    return Response(content=make_thumbnail(orig, max_side=800),
                    media_type="image/jpeg")


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


def _item_urls(item: dict) -> dict:
    """清单条目 url/preview_url 组装：唯一实现，仅 /content 与 /display/current
    两处调用（ADR-0002：源不负责 url，故为路由层 helper 而非源方法）。

    统一指向 /api/images/{id}/raw|preview——/{id}/raw 是转正的唯一设备
    下载契约（五源经内容源注册表分发）；自由模块 url 不再带 ?module=
    查询参数（源 render() 服务当前内容，与 /content 选同一模块）。
    """
    item["url"] = f"/api/images/{item['id']}/raw"
    item["preview_url"] = f"/api/images/{item['id']}/preview"
    return item


def _content_pushed() -> dict | None:
    """当日有效的内容库推送图（清单条目，不含 url——由统一 helper 组装）；
    无则 None。内容库进清单走注入机制而非源 meta()（内容库没有
    「当前内容」概念，见 ADR-0002）。"""
    p = runtime_config.get("content_push") or {}
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
    }


@router.get("/content/pushed")
async def content_pushed():
    p = runtime_config.get("content_push") or {}
    return {"pushed": p}


@router.post("/content/push")
async def content_push(body: dict):
    """将内容库某张图推送到显示（当日有效，次日自动回落）。"""
    img_id = (body.get("id") or "").strip()
    if not img_id:
        raise HTTPException(400, "missing id")
    if not db.get_image(img_id):
        raise HTTPException(404, "image not found")
    runtime_config.save(
        {"content_push": {"id": img_id, "date": datetime.now().strftime("%Y-%m-%d")}})
    return {"ok": True, "id": img_id}


@router.delete("/content/push")
async def content_unpush():
    runtime_config.save({"content_push": {}})
    return {"ok": True}


@router.get("/content")
async def content_list(device_id: str | None = None):
    """内容清单：设备轮询此接口。

    各分支消费内容源的元数据方法（meta()，ADR-0002），url/preview_url 由
    模块级 helper _item_urls 统一组装为 /api/images/{id}/raw|preview 形态
    （/{id}/raw 是唯一设备下载契约，五源经注册表分发）。

    优先级：临时显示（display，一次性上传切换）> 时段编排（见 app.slots，
    slot_segments 分段，未配置时兼容旧 slot_weather / slot_photo / slot_free）：
      weather 时段 → 天气+日历卡片（QWEATHER_KEY 未配/失败时回退照片）
      photo    时段 → 每日精选照片
      free     时段 → 自由模块卡片（LLM+文生图每日生成；依赖未配置时回退照片）
    可附带设备 id 上报状态。
    """
    item = None
    source = ""

    # 临时显示（一次性上传切换）优先于一切时段内容
    meta = display.SOURCE.meta()
    if meta:
        item, source = meta, "display"
    else:
        slot = slots.current_slot()
        source = slot

        if slot == slots.SLOT_WEATHER:
            item = weather_card.SOURCE.meta()
        elif slot == slots.SLOT_FREE:
            item = free_module.SOURCE.meta()

        if item is None:
            # 照片时段，或天气/自由模块不可用时的回退。
            # 照片时段优先级：内容库「推送到显示」的图 > 手动指定今日精选 > 每日精选。
            # 内容库未推送的图只入库不上屏（须在内容库点「推送到显示」才上屏）。
            if slot == slots.SLOT_PHOTO:
                pushed = _content_pushed()
                if pushed:
                    item, source = pushed, "content_push"
            if item is None:
                meta = daily.SOURCE.meta()
                if meta:
                    item, source = meta, "daily"

    result = [_item_urls(item)] if item else []

    # 设备附带上报心跳（与 /heartbeat 等价，减少一次请求）
    if device_id:
        db.upsert_device(device_id, current_image=result[0]["id"] if result else "",
                         last_seen=db.now())
    return {"images": result, "source": source}
