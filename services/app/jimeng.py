"""jimeng.py — 即梦 AI 图片生成 4.6（火山引擎视觉智能，异步任务式）。

流程：
  1. CVSync2AsyncSubmitTask 提交任务 → data.task_id
  2. CVSync2AsyncGetResult 轮询 → data.status == done → data.image_urls[]
  3. 下载图片字节

鉴权：火山引擎 V4 签名（Region=cn-north-1, Service=cv，AK/SK）。
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

logger = logging.getLogger(__name__)

HOST = "visual.volcengineapi.com"
ENDPOINT = f"https://{HOST}"
REGION = "cn-north-1"
SERVICE = "cv"
VERSION = "2022-08-31"
REQ_KEY = "jimeng_seedream46_cvtob"

# 竖屏相框推荐尺寸（面积在 1K~2K 之间，宽高比接近 3:4）
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
    ak, sk = settings.jimeng_access_key, settings.jimeng_secret_key
    if not (ak and sk):
        raise ValueError("jimeng keys not configured")
    query = urllib.parse.urlencode({"Action": action, "Version": VERSION})
    payload = json.dumps(body)
    headers = _sign_v4(ak, sk, query, payload)
    r = requests.post(f"{ENDPOINT}?{query}", headers=headers, data=payload, timeout=60)
    r.raise_for_status()
    result = json.loads(r.text.replace("\\u0026", "&"))
    if result.get("code") != 10000:
        raise RuntimeError(f"jimeng {action}: {result.get('message')} (code={result.get('code')})")
    return result


def _submit(prompt: str, width: int, height: int) -> str:
    result = _api("CVSync2AsyncSubmitTask", {
        "req_key": REQ_KEY, "prompt": prompt,
        "width": width, "height": height,
        "force_single": True, "return_url": True,
    })
    task_id = (result.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"jimeng no task_id: {result}")
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
            raise RuntimeError(f"jimeng task {status}")
        time.sleep(_POLL_INTERVAL_S)
    raise TimeoutError(f"jimeng task timeout after {timeout_s}s")


def generate_image(prompt: str, ratio: str = "3:4", timeout: int = 180) -> bytes | None:
    """生成图片，返回图片字节。失败返回 None（已记日志）。"""
    try:
        w, h = RATIOS.get(ratio, (1296, 1728))
        task_id = _submit(prompt, w, h)
        logger.info("jimeng task submitted: %s", task_id)
        urls = _poll(task_id, timeout_s=timeout)
        url = urls[0]
        if url.startswith("data:"):
            import base64
            return base64.b64decode(url.split(",", 1)[1])
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        return r.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("jimeng generate failed: %s", exc)
        return None


def generate_cartoon_for_news(titles: list[str], ratio: str = "3:4") -> bytes | None:
    """根据新闻标题生成卡通风图片。返回图片字节或 None。"""
    top = titles[0] if titles else "今日热点"
    rest = "；".join(titles[1:4])
    prompt = (f"温馨可爱的卡通插画风格（扁平、暖色调、圆润），概括这条新闻：{top}。"
              f"其他相关热点：{rest}。无文字，无水印，竖构图。")
    return generate_image(prompt, ratio=ratio)
