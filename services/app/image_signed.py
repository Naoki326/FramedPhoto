"""image_signed.py — 签名式异步文生图接入（提交任务 + 轮询取图）。

流程：
  1. 提交任务 → data.task_id
  2. 轮询任务 → data.status == done → data.image_urls[]
  3. 下载图片字节

鉴权：厂商 V4 签名（HMAC-SHA256，Region / Service / 请求 Action 随厂商）。
本文件是**唯一**与具体签名式厂家绑定的地方（ENDPOINT / REGION / SERVICE /
REQ_KEY / ACTION 名）：换厂家只改这组模块常量，配置面（app/llm.py：
image_api / image_signed_ak / image_signed_sk）与调用方均不感知厂家。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import time
import urllib.parse

import requests

from app.config import settings
from app import llm

logger = logging.getLogger(__name__)

# ── 厂家绑定：换签名式文生图厂家只改这一段 ──
HOST = "visual.volcengineapi.com"
ENDPOINT = f"https://{HOST}"
REGION = "cn-north-1"
SERVICE = "cv"
VERSION = "2022-08-31"
REQ_KEY = "jimeng_seedream46_cvtob"

# 相框横放观看，横屏比例：4:3（推荐）或 16:9；面积在 1K~2K 之间
RATIOS = {"4:3": (1728, 1296), "3:4": (1296, 1728), "16:9": (2048, 1152), "9:16": (1152, 2048)}

_POLL_INTERVAL_S = 4
_POLL_TIMEOUT_S = 180


def _sign_v4(access_key: str, secret_key: str, query: str, body: str) -> dict:
    """火山引擎 V4 签名，返回请求头。"""
    now = dt.datetime.now(dt.timezone.utc)
    current_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    payload_hash = hashlib.sha256(body.encode()).hexdigest()
    canonical_headers = (f"content-type:application/json\n"
                         f"host:{HOST}\n"
                         f"x-content-sha256:{payload_hash}\n"
                         f"x-date:{current_date}\n")
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = "\n".join([
        "POST", "/", query, canonical_headers, signed_headers, payload_hash,
    ])

    credential_scope = f"{datestamp}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256", current_date, credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(secret_key.encode(), datestamp)
    k_region = _hmac(k_date, REGION)
    k_service = _hmac(k_region, SERVICE)
    k_signing = _hmac(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    authorization = (f"HMAC-SHA256 Credential={access_key}/{credential_scope}, "
                     f"SignedHeaders={signed_headers}, Signature={signature}")
    return {
        "X-Date": current_date,
        "Authorization": authorization,
        "X-Content-Sha256": payload_hash,
        "Content-Type": "application/json",
    }


def _api(action: str, body: dict) -> dict:
    """调用视觉智能 API，返回 JSON。签名失败/异常抛错。"""
    ak, sk = llm.image_signed_conf()
    if not (ak and sk):
        raise ValueError("signed image API keys not configured")
    query = urllib.parse.urlencode({"Action": action, "Version": VERSION})
    payload = json.dumps(body)
    headers = _sign_v4(ak, sk, query, payload)
    r = requests.post(f"{ENDPOINT}?{query}", headers=headers, data=payload, timeout=60)
    r.raise_for_status()
    result = json.loads(r.text.replace("\\u0026", "&"))
    if result.get("code") != 10000:
        raise RuntimeError(f"image_signed {action}: {result.get('message')} (code={result.get('code')})")
    return result


def _submit(prompt: str, width: int, height: int) -> str:
    result = _api("CVSync2AsyncSubmitTask", {
        "req_key": REQ_KEY, "prompt": prompt,
        "width": width, "height": height,
        "force_single": True, "return_url": True,
    })
    task_id = (result.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"image_signed no task_id: {result}")
    return task_id


def _poll(task_id: str, timeout_s: int = _POLL_TIMEOUT_S) -> list[str]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = _api("CVSync2AsyncGetResult", {"req_key": REQ_KEY, "task_id": task_id})
        data = result.get("data") or {}
        status = data.get("status")
        if status == "done":
            urls = data.get("image_urls") or []
            if urls:
                return urls
            b64s = data.get("binary_data_base64") or []
            if b64s:
                import base64
                return [f"data:image/png;base64,{b64s[0]}"]
        if status in ("not_found", "expired"):
            raise RuntimeError(f"image_signed task {status}")
        time.sleep(_POLL_INTERVAL_S)
    raise TimeoutError(f"image_signed task timeout after {timeout_s}s")


def generate_image(prompt: str, ratio: str | None = None, timeout: int = 180) -> bytes | None:
    """生成图片，返回图片字节。失败返回 None（已记日志）。

    ratio 默认取配置 image_ratio（横屏 4:3）——生成的横图在 prepare_image
    时旋转 90°，相框横放观看即正立。
    """
    try:
        ratio = ratio or llm.image_ratio()
        w, h = RATIOS.get(ratio, (1728, 1296))
        task_id = _submit(prompt, w, h)
        logger.info("image_signed task submitted: %s", task_id)
        urls = _poll(task_id, timeout_s=timeout)
        url = urls[0]
        if url.startswith("data:"):
            import base64
            return base64.b64decode(url.split(",", 1)[1])
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        return r.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_signed generate failed: %s", exc)
        return None


def generate_cartoon_for_news(titles: list[str], ratio: str | None = None) -> bytes | None:
    """根据新闻标题生成卡通风图片。返回图片字节或 None。"""
    top = titles[0] if titles else "今日热点"
    rest = "；".join(titles[1:4])
    prompt = (f"温馨可爱的卡通插画风格（扁平、暖色调、圆润），概括这条新闻：{top}。"
              f"其他相关热点：{rest}。无文字，无水印，横构图。")
    return generate_image(prompt, ratio=ratio)
