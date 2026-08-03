"""
analyzer.py — 照片 AI 分析与评分。

设计（原创实现，思路参考开源社区同类方案）：
  1. 有 VLM 服务（OpenAI 兼容接口，如 LM Studio / 云端）时：视觉大模型
     生成描述 / 类型 / 回忆度 / 美观度 / 文案，返回结构化 JSON
  2. 无 VLM 或调用失败时：降级为启发式评分（清晰度、构图、色彩多样性），
     保证整套流程离线也能跑通（AI 是增强，不是必需）
  3. EXIF 提取拍摄时间 / GPS，供「历史上的今天」与旅行识别使用

VLM 配置（.env）：
  VLM_API_URL=http://127.0.0.1:1234/v1/chat/completions
  VLM_API_KEY=
  VLM_MODEL=qwen3-vl-32b-instruct
  VLM_ENABLED=true        # false 时直接走启发式
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from dataclasses import dataclass, field

import requests
from PIL import Image, ImageStat

from app.config import settings

SYSTEM_PROMPT = """你是一名家庭相册策展人。请分析这张照片，严格返回 JSON（不要 Markdown）：
{
  "description": "30字以内的画面描述",
  "type": "人像|风景|美食|聚会|宠物|建筑|旅行|日常|其他",
  "memory_score": 0-100的整数，按"值得再次被看见/情感价值"打分,
  "beauty_score": 0-100的整数，按画面美观度打分,
  "caption": "一句20字以内、有人情味的文案（不要引用原图文字）",
  "reason": "一句话说明评分理由"
}"""


@dataclass
class Analysis:
    path: str
    filename: str
    description: str = ""
    type: str = "其他"
    memory_score: float = 0.0
    beauty_score: float = 0.0
    caption: str = ""
    reason: str = ""
    source: str = "heuristic"      # vlm | heuristic
    shot_at: float | None = None   # EXIF 拍摄时间戳
    shot_source: str = "file"      # exif | file
    gps_lat: float | None = None
    gps_lon: float | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "filename": self.filename,
            "description": self.description,
            "type": self.type,
            "memory_score": self.memory_score,
            "beauty_score": self.beauty_score,
            "caption": self.caption,
            "reason": self.reason,
            "source": self.source,
            "shot_at": self.shot_at,
            "shot_source": self.shot_source,
            "gps_lat": self.gps_lat,
            "gps_lon": self.gps_lon,
        }


# ---------- EXIF ----------

def _exif_datetime_to_ts(value: str) -> float | None:
    """'2023:05:14 16:30:22' -> unix 时间戳。"""
    m = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(value))
    if not m:
        return None
    try:
        import calendar
        y, mo, d, h, mi, s = map(int, m.groups())
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None
        return calendar.timegm((y, mo, d, h, mi, s))
    except (ValueError, OverflowError):
        return None


def extract_exif(path: str) -> tuple[float | None, float | None, float | None, str]:
    """返回 (拍摄时间戳, gps_lat, gps_lon, shot_source)。

    shot_source: 'exif' 表示 EXIF 真实拍摄时间；'file' 表示兜底用文件修改时间。
    """
    shot_at: float | None = None
    gps_lat = gps_lon = None
    source = "file"
    try:
        img = Image.open(path)
        exif = img.getexif()
        if exif:
            # 优先 EXIF 真实拍摄时间
            exif_time = None
            dt = exif.get(0x0132)  # DateTimeOriginal
            if dt:
                exif_time = _exif_datetime_to_ts(dt)
            if exif_time is None:
                dto = exif.get(0x9003)  # DateTimeDigitized
                if dto:
                    exif_time = _exif_datetime_to_ts(dto)
            if exif_time is not None:
                shot_at, source = exif_time, "exif"

            gps = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else None
            if gps and 2 in gps and 4 in gps:
                lat = _dms_to_deg(gps[2], gps.get(1, "N"))
                lon = _dms_to_deg(gps[4], gps.get(3, "E"))
                if lat is not None and lon is not None:
                    gps_lat, gps_lon = lat, lon
        if shot_at is None:
            shot_at = os.path.getmtime(path)  # 兜底：文件修改时间
    except Exception:
        if shot_at is None:
            try:
                shot_at = os.path.getmtime(path)
            except OSError:
                pass
    return shot_at, gps_lat, gps_lon, source


def _dms_to_deg(dms, ref) -> float | None:
    try:
        d, m, s = dms
        deg = float(d) + float(m) / 60.0 + float(s) / 3600.0
        if ref in ("S", "W"):
            deg = -deg
        return deg
    except (TypeError, ValueError):
        return None


# ---------- 启发式评分（无 VLM 降级） ----------

def heuristic_score(path: str) -> tuple[float, float, str]:
    """基于图像统计的启发式评分，返回 (memory_score, beauty_score, reason)。

    维度：清晰度（拉普拉斯方差）、构图（中心权重）、色彩（饱和度/多样性）、亮度合理区间。
    不追求完美，只求在没有 AI 服务时仍有合理排序。
    """
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return 40.0, 40.0, "无法解码，按中等分值处理"

    # 缩小加速
    img = img.copy()
    img.thumbnail((400, 400))

    gray = img.convert("L")
    stat = ImageStat.Stat(gray)
    mean, stddev = stat.mean[0], stat.stddev[0]

    # 清晰度：拉普拉斯近似 = 二阶差分（用 stddev 粗略代表细节量）
    sharp = min(100, stddev * 3.0)

    # 色彩饱和度
    hsv = img.convert("HSV")
    s_stat = ImageStat.Stat(hsv)
    sat = min(100, s_stat.mean[1] * 0.5)

    # 亮度合理区间（过曝/欠曝惩罚）
    lum = mean
    lum_score = 100 if 60 <= lum <= 200 else max(20, 100 - abs(lum - 128) * 0.6)

    beauty = round(0.45 * sharp + 0.35 * sat + 0.20 * lum_score, 1)

    # 回忆度：启发式里取 美观度 的中性映射（真实情感分靠 VLM）
    memory = round(min(90, 40 + 0.4 * beauty), 1)
    reason = f"启发式评分：清晰度 {sharp:.0f}，色彩 {sat:.0f}，亮度 {lum_score:.0f}"
    return memory, beauty, reason


# ---------- VLM 分析 ----------

class VLMUnavailable(Exception):
    pass


def _call_vlm(image_bytes: bytes) -> dict:
    """调用 OpenAI 兼容 VLM，返回解析后的 JSON dict。"""
    url = settings.vlm_api_url
    if not url:
        raise VLMUnavailable("VLM_API_URL not configured")

    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": settings.vlm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": "请分析这张照片。"},
            ]},
        ],
        "temperature": 0.7,
        "max_tokens": 600,
    }
    headers = {"Content-Type": "application/json"}
    if settings.vlm_api_key:
        headers["Authorization"] = f"Bearer {settings.vlm_api_key}"

    resp = requests.post(url, json=payload, headers=headers, timeout=settings.vlm_timeout)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    # 容错：从响应中提取 JSON 块
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise VLMUnavailable("VLM response contains no JSON")
    return json.loads(m.group(0))


def analyze_image(path: str, use_vlm: bool | None = None) -> Analysis:
    """分析单张照片。use_vlm=None 时按配置；失败自动降级启发式。"""
    filename = os.path.basename(path)
    shot_at, gps_lat, gps_lon, shot_source = extract_exif(path)

    want_vlm = settings.vlm_enabled if use_vlm is None else use_vlm

    if want_vlm:
        try:
            with open(path, "rb") as f:
                raw = f.read()
            # VLM 需要的图片控制在 2MB 内（base64 后 ~2.7MB）
            if len(raw) > 2 * 1024 * 1024:
                img = Image.open(io.BytesIO(raw))
                img.thumbnail((1600, 1600))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=90)
                raw = buf.getvalue()

            result = _call_vlm(raw)
            return Analysis(
                path=path, filename=filename, source="vlm",
                description=str(result.get("description", "")).strip(),
                type=str(result.get("type", "其他")).strip() or "其他",
                memory_score=float(result.get("memory_score", 50)),
                beauty_score=float(result.get("beauty_score", 50)),
                caption=str(result.get("caption", "")).strip(),
                reason=str(result.get("reason", "")).strip(),
                shot_at=shot_at, shot_source=shot_source, gps_lat=gps_lat, gps_lon=gps_lon,
            )
        except Exception as exc:
            # 降级到启发式
            memory, beauty, reason = heuristic_score(path)
            return Analysis(
                path=path, filename=filename, source="heuristic",
                memory_score=memory, beauty_score=beauty,
                reason=f"VLM 不可用（{exc.__class__.__name__}），已降级启发式评分",
                caption="", shot_at=shot_at, shot_source=shot_source,
                gps_lat=gps_lat, gps_lon=gps_lon,
            )

    memory, beauty, reason = heuristic_score(path)
    return Analysis(
        path=path, filename=filename, source="heuristic",
        memory_score=memory, beauty_score=beauty, reason=reason,
        shot_at=shot_at, shot_source=shot_source, gps_lat=gps_lat, gps_lon=gps_lon,
    )
