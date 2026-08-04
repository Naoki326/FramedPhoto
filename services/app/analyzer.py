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
import logging
import os
import re
import time
from dataclasses import dataclass, field

import requests
from PIL import Image, ImageStat

from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一名家庭相册策展人。请分析这张照片，严格返回 JSON（不要 Markdown）：
{
  "description": "30字以内的画面描述",
  "type": "人像|风景|美食|聚会|宠物|建筑|旅行|日常|其他",
  "memory_score": 0-100的整数，按"值得再次被看见/情感价值"打分,
  "beauty_score": 0-100的整数，按画面美观度打分,
  "caption": "一句20字以内、有人情味的文案（不要引用原图文字）",
  "reason": "一句话说明评分理由"
}"""

CAPTION_PROMPT = """你是一名家庭相册策展人。看这张照片，写一句有温度的中文文案（不超过 30 字）。
要求：每次生成的角度、语气、切入点都要随机变化——有时写场景、有时写人物、有时写情绪、有时写时间感；避免重复用过的句式。
只输出文案本身，不要引号、不要解释、不要 Markdown。"""


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


def _parse_json_block(text: str) -> dict:
    """容错：从模型响应中提取 JSON 块并解析。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise VLMUnavailable("VLM response contains no JSON")
    return json.loads(m.group(0))


def _proxies() -> dict | None:
    """从配置构造 requests 代理（空则 None = 直连）。"""
    if not settings.vlm_proxy:
        return None
    return {"http": settings.vlm_proxy, "https": settings.vlm_proxy}


class OpenAICompatClient:
    """OpenAI Chat Completions 接口（/v1/chat/completions）。"""

    def __init__(self, url: str, api_key: str, model: str, timeout: int):
        self.url, self.api_key, self.model, self.timeout = url, api_key, model, timeout

    def analyze(self, image_bytes: bytes) -> dict:
        if not self.url:
            raise VLMUnavailable("VLM_API_URL not configured")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
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
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = requests.post(self.url, json=payload, headers=headers,
                             timeout=self.timeout, proxies=_proxies())
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse_json_block(content)

    def generate_caption(self, image_bytes: bytes) -> str:
        """随机文案（不要求 JSON，仅返回一句话）。"""
        if not self.url:
            raise VLMUnavailable("VLM_API_URL not configured")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": CAPTION_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]},
            ],
            "temperature": 1.1,
            "max_tokens": 120,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = requests.post(self.url, json=payload, headers=headers,
                             timeout=self.timeout, proxies=_proxies())
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


class OpenAIResponsesClient:
    """OpenAI Responses API（/v1/responses）—— GPT-5.x 等新模型专用端点。

    OpenCode Go 的 gpt-5.6-luna 仅在 responses 端点可用，且部分区域
    需经代理访问。
    """

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int):
        base_url = base_url.rstrip("/")
        # 兼容完整 chat/completions URL 与 base URL 两种写法
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
        self.base_url = base_url
        self.api_key, self.model, self.timeout = api_key, model, timeout

    @property
    def url(self) -> str:
        return f"{self.base_url}/responses"

    def analyze(self, image_bytes: bytes) -> dict:
        if not self.api_key:
            raise VLMUnavailable("VLM_API_KEY not configured")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
                    {"type": "input_text", "text": "请分析这张照片。"},
                ],
            }],
            "max_output_tokens": 1000,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = requests.post(self.url, json=payload, headers=headers,
                             timeout=self.timeout, proxies=_proxies())
        resp.raise_for_status()
        data = resp.json()
        # output 数组：找 type=message 的内容文本
        text = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text += c.get("text", "")
        if not text:
            raise VLMUnavailable("Responses API returned empty content")
        return _parse_json_block(text)

    def generate_caption(self, image_bytes: bytes) -> str:
        """随机文案（不要求 JSON，仅返回一句话）。"""
        if not self.api_key:
            raise VLMUnavailable("VLM_API_KEY not configured")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "instructions": CAPTION_PROMPT,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
                ],
            }],
            "max_output_tokens": 120,
            "temperature": 1.1,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = requests.post(self.url, json=payload, headers=headers,
                             timeout=self.timeout, proxies=_proxies())
        resp.raise_for_status()
        data = resp.json()
        text = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text += c.get("text", "")
        return text.strip()


class AnthropicClient:
    """Anthropic Messages API（Claude 视觉模型）。"""

    def __init__(self, api_key: str, model: str, timeout: int, base_url: str = "https://api.anthropic.com"):
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self.base_url = base_url.rstrip("/")

    def analyze(self, image_bytes: bytes) -> dict:
        if not self.api_key:
            raise VLMUnavailable("ANTHROPIC_API_KEY not configured")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "max_tokens": 600,
            "system": SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    }},
                    {"type": "text", "text": "请分析这张照片。"},
                ],
            }],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        resp = requests.post(f"{self.base_url}/v1/messages", json=payload,
                             headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"]
        return _parse_json_block(text)

    def generate_caption(self, image_bytes: bytes) -> str:
        """随机文案（不要求 JSON，仅返回一句话）。"""
        if not self.api_key:
            raise VLMUnavailable("ANTHROPIC_API_KEY not configured")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "max_tokens": 120,
            "temperature": 1.1,
            "system": CAPTION_PROMPT,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    }},
                ],
            }],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        resp = requests.post(f"{self.base_url}/v1/messages", json=payload,
                             headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()


def build_client() -> AnthropicClient | OpenAICompatClient | OpenAIResponsesClient | None:
    """按 VLM_PROVIDER / VLM_API_MODE 决策返回客户端；无法使用时返回 None。

    auto：有 ANTHROPIC_API_KEY 优先 Claude，其次 OpenAI 兼容，都没有则 None。
    mode=responses 时返回 OpenAIResponsesClient（/v1/responses）。
    """
    provider = settings.vlm_provider.strip().lower()
    if provider == "disabled":
        return None

    def _openai():
        if settings.vlm_api_mode.strip().lower() == "responses":
            return OpenAIResponsesClient(settings.vlm_api_url, settings.vlm_api_key,
                                         settings.vlm_model, settings.vlm_timeout)
        return OpenAICompatClient(settings.vlm_api_url, settings.vlm_api_key,
                                  settings.vlm_model, settings.vlm_timeout)

    if provider == "anthropic":
        return AnthropicClient(settings.anthropic_api_key, settings.anthropic_model,
                               settings.vlm_timeout, settings.anthropic_base_url)
    if provider == "openai":
        return _openai()
    # auto
    if settings.anthropic_api_key:
        return AnthropicClient(settings.anthropic_api_key, settings.anthropic_model,
                               settings.vlm_timeout, settings.anthropic_base_url)
    if settings.vlm_api_url:
        return _openai()
    return None


def generate_random_caption(path: str) -> str:
    """对单张照片生成随机文案（每日渲染时按需调用）。

    每次调用 prompt 要求随机角度；VLM 不可用/失败时返回空字符串（调用方回退）。
    """
    if not settings.vlm_enabled:
        return ""
    client = build_client()
    if client is None:
        return ""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) > 2 * 1024 * 1024:
            img = Image.open(io.BytesIO(raw))
            img.thumbnail((1600, 1600))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            raw = buf.getvalue()
        return client.generate_caption(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("generate_random_caption failed: %s", exc.__class__.__name__)
        return ""


def analyze_image(path: str, use_vlm: bool | None = None) -> Analysis:
    """分析单张照片。use_vlm=None 时按配置；失败自动降级启发式。"""
    filename = os.path.basename(path)
    shot_at, gps_lat, gps_lon, shot_source = extract_exif(path)

    want_vlm = settings.vlm_enabled if use_vlm is None else use_vlm

    if want_vlm:
        try:
            client = build_client()
            if client is None:
                raise VLMUnavailable("no VLM configured (VLM_API_URL / ANTHROPIC_API_KEY)")

            with open(path, "rb") as f:
                raw = f.read()
            # VLM 需要的图片控制在 2MB 内（base64 后 ~2.7MB）
            if len(raw) > 2 * 1024 * 1024:
                img = Image.open(io.BytesIO(raw))
                img.thumbnail((1600, 1600))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=90)
                raw = buf.getvalue()

            result = client.analyze(raw)
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
