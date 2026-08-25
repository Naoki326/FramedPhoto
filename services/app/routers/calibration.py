"""calibration.py — 管理台「屏幕校准」API。

- GET    ""            校准状态（六色采样值 / 是否已校准 / 生效 profile）
- POST   "/generate"   生成校准图并直推设备（一键）
- GET    "/chart"      校准图 PNG 预览
- POST   "/photo"      上传校准照片 -> 采样六色 -> 写 calibrated.json -> 热加载
- POST   "/photo/marked" 上传照片并返回采样区标注图（供核对采样位置）
- DELETE ""            清除校准，恢复默认占位
"""
import re

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from app import calibration, display
from app.config import settings
from app.epd_image import parse_fps6
from app.routers.images import _classify_fps6_error

router = APIRouter()


def _validate_fps6(raw: bytes, expected: tuple[int, int] | None = None) -> tuple[int, int]:
    """校准直推路径的帧校验：统一走 parse_fps6，保留既有 400 语义与中文消息。"""
    try:
        prepared = parse_fps6(raw, expected_size=expected)
    except ValueError as exc:
        kind, actual = _classify_fps6_error(str(exc))
        if kind == "magic":
            raise HTTPException(400, "不是有效的 FPS6 文件") from exc
        if kind == "size":
            raise HTTPException(
                400, f"尺寸必须为 {expected[0]}x{expected[1]}，"
                     f"实际 {actual[0]}x{actual[1]}") from exc
        raise HTTPException(400, "FPS6 数据不完整") from exc
    return prepared.width, prepared.height


@router.get("")
async def status():
    return calibration.calibration_status()


@router.post("/generate")
async def generate_and_push():
    """生成校准图，写入 display 通道直推设备（绕过量化）。"""
    raw = calibration.chart_fps6()
    _validate_fps6(raw, (settings.epd_width, settings.epd_height))
    meta = display.save_pushed_frame(raw, "calibration.fps6")
    return {"ok": True, "display": meta, **calibration.calibration_status()}


@router.get("/chart")
async def chart_preview():
    """校准图 PNG 预览（横放视角，供浏览器查看）。"""
    return Response(content=calibration.chart_png(), media_type="image/png")


@router.get("/rainbow")
async def rainbow_preview():
    """彩虹效果图 PNG 预览（横放视角，未量化的源图）。"""
    return Response(content=calibration.rainbow_png(), media_type="image/png")


@router.get("/rainbow/preview")
async def rainbow_quantized_preview(bias: float = 0.0):
    """彩虹效果图量化后预览：走设备同款链路（v2 量化+device 色+浓彩偏置）。

    bias 直接取查询参数（预览的是滑杆当前值，未保存也能看）；非法值抳
    0..4。降采样 600x800 加速交互（见 rainbow_quantized_preview_png）。
    """
    from app.epd_image import MAX_CHROMA_BIAS
    bias = max(0.0, min(MAX_CHROMA_BIAS, float(bias)))
    return Response(content=calibration.rainbow_quantized_preview_png(bias),
                    media_type="image/png")


@router.post("/rainbow/push")
async def rainbow_push():
    """生成彩虹效果图并直推设备（走正常量化链路，含浓彩偏置）。"""
    raw = calibration.rainbow_fps6()
    _validate_fps6(raw, (settings.epd_width, settings.epd_height))
    return {"ok": True, "display": display.save_pushed_frame(raw, "rainbow.fps6")}


@router.put("/chroma-bias")
async def set_chroma_bias(body: dict):
    """设置浓彩偏置（0..4）：色域外高饱和色的墨水对混色向彩色端偏移。

    0 = 忠实混色（最小二乘比例，默认）；越大越浓彩（偏暗）。存
    runtime_config，全部渲染链路即时生效（见 ADR-0004）。
    """
    from app import runtime_config
    from app.epd_image import MAX_CHROMA_BIAS
    try:
        value = float(body.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(400, "value 必须是数字") from None
    if not 0.0 <= value <= MAX_CHROMA_BIAS:
        raise HTTPException(400, f"value 必须在 0~{MAX_CHROMA_BIAS:.0f} 之间")
    runtime_config.save({"chroma_bias": value})
    return {"ok": True, "chroma_bias": value}


@router.post("/photo")
async def upload_photo(file: UploadFile = File(...), mode: str = "sample", rotate: int = 0):
    """上传校准照片并采样六色（mode=sample，默认，全自动）。

    - rotate: 0/90/180/270，照片中校准图方向不对时旋转（逆时针）。
    - mode=pick：返回照片与采样区标注（前端显示照片供人工点选）。
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    from PIL import Image
    import io as _io
    try:
        img = Image.open(_io.BytesIO(data))
        if rotate:
            img = img.rotate(rotate, expand=True)
        if mode == "sample":
            device = calibration.sample_photo(img)
        elif mode == "pick":
            # 返回照片（旋转后）供前端点选，不自动采样
            buf = _io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return Response(content=buf.getvalue(), media_type="image/png")
        else:
            raise HTTPException(400, f"unknown mode {mode!r}")
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:
        raise HTTPException(422, f"图片解析失败: {e}") from e
    payload = calibration.save_calibrated(device)
    return {"ok": True, **payload, "status": calibration.calibration_status()}


@router.post("/pick/color")
async def pick_color(body: dict):
    """照片点选取色：body={"photo_b64": ..., "x": float, "y": float}。

    x/y 为照片归一化坐标（0..1，以旋转后照片为准）。返回该点邻域
    （约 15x15 px）的平均色，供人工逐色匹配后调用 /device 写入。
    """
    import base64 as _b64
    import io as _io
    from PIL import Image
    data = body.get("photo_b64", "")
    if not data:
        raise HTTPException(400, "missing photo_b64")
    try:
        raw = _b64.b64decode(data)
        img = Image.open(_io.BytesIO(raw)).convert("RGB")
        x, y = float(body.get("x", 0)), float(body.get("y", 0))
        px = img.load()
        cx, cy = int(x * img.width), int(y * img.height)
        r, g, b, n = 0, 0, 0, 0
        R = 7
        for dy in range(-R, R + 1):
            for dx in range(-R, R + 1):
                xx, yy = cx + dx, cy + dy
                if 0 <= xx < img.width and 0 <= yy < img.height:
                    pr, pg, pb = px[xx, yy][:3]
                    r += pr; g += pg; b += pb; n += 1
        return {"ok": True, "color": [round(r / n), round(g / n), round(b / n)]}
    except Exception as e:
        raise HTTPException(422, f"取色失败: {e}") from e


@router.post("/device")
async def set_device(body: dict):
    """人工校准：直接设置六色 device 值并写入 calibrated.json（热加载）。

    body: {"device": [[r,g,b] x6]}  # 顺序同 NIBBLES：[黑,白,黄,红,蓝,绿]
    """
    from pydantic import BaseModel

    class Body(BaseModel):
        device: list[list[int]]

    b = Body(**body)
    if len(b.device) != 6 or not all(len(c) == 3 for c in b.device):
        raise HTTPException(400, "device 必须为 6 组 [r,g,b]")
    for c in b.device:
        for v in c:
            if not (0 <= v <= 255):
                raise HTTPException(400, "颜色值必须在 0..255")
    payload = calibration.save_calibrated(b.device)
    return {"ok": True, **payload, "status": calibration.calibration_status()}


@router.post("/photo/marked")
async def upload_photo_marked(file: UploadFile = File(...)):
    """上传校准照片并返回采样区标注图（红框），供核对采样位置。"""
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    from PIL import Image, ImageDraw, ImageFont
    import io as _io
    try:
        img = Image.open(_io.BytesIO(data)).convert("RGB")
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default(24)
        for i, (x0, y0, x1, y1) in enumerate(calibration.band_center_fracs()):
            box = (int(x0 * img.width), int(y0 * img.height),
                   int(x1 * img.width), int(y1 * img.height))
            draw.rectangle(box, outline=(255, 0, 0), width=6)
            draw.text((box[0] + 4, box[1] + 4), f"band {i}", fill=(255, 0, 0), font=font)
    except Exception as e:
        raise HTTPException(422, f"图片解析失败: {e}") from e
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.delete("")
async def clear():
    removed = calibration.clear_calibrated()
    return {"ok": True, "removed": removed, "status": calibration.calibration_status()}
