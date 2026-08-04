"""jimeng.py — 即梦 AI 图片生成（火山引擎视觉智能 API）。

端点：https://visual.volcengineapi.com?Action=CVProcess&Version=2022-08-31
鉴权：火山引擎 V4 签名（HMAC-SHA256，AK/SK）
请求体：{req_key, prompt, return_url, width, height}
响应：data.image_urls[0]（图片 URL，有时效，需及时下载）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import io
import json
import logging
import urllib.parse

import requests
from PIL import Image

from app.config import settings

logger = logging.getLogger(__name__)

HOST = "visual.volcengineapi.com"
ENDPOINT = f"https://{HOST}"
REGION = "cn-north-1"
SERVICE = "cv"

# 即梦图片生成 req_key（图片生成 4.x 系列）
REQ_KEY = "jimeng_high_aes_general_v21_L"

# 比例 → 尺寸（竖屏相框用 3:4 或 9:16）
RATIOS = {"4:3": (512, 384), "3:4": (384, 512), "16:9": (512, 288), "9:16": (288, 512)}


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


def generate_image(prompt: str, ratio: str = "3:4", timeout: int = 180) -> bytes | None:
    """调用即梦生成图片，返回 PNG/JPEG 字节。失败返回 None。"""
    ak, sk = settings.jimeng_access_key, settings.jimeng_secret_key
    if not (ak and sk):
        logger.warning("jimeng keys not configured")
        return None
    w, h = RATIOS.get(ratio, (384, 512))

    query = urllib.parse.urlencode({"Action": "CVProcess", "Version": "2022-08-31"})
    body = json.dumps({
        "req_key": REQ_KEY, "prompt": prompt, "return_url": True,
        "width": w, "height": h,
    })
    try:
        headers = _sign_v4(ak, sk, query, body)
        r = requests.post(f"{ENDPOINT}?{query}", headers=headers, data=body, timeout=timeout)
        r.raise_for_status()
        result = json.loads(r.text.replace("\\u0026", "&"))
        meta = result.get("ResponseMetadata", {})
        if meta.get("Error"):
            logger.warning("jimeng api error: %s", meta["Error"].get("Message"))
            return None
        urls = (result.get("data") or {}).get("image_urls") or []
        if not urls:
            logger.warning("jimeng no image url: %s", str(result)[:200])
            return None
        img_r = requests.get(urls[0], timeout=timeout)
        img_r.raise_for_status()
        return img_r.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("jimeng generate failed: %s", exc.__class__.__name__)
        return None


def generate_cartoon_for_news(titles: list[str], ratio: str = "3:4") -> bytes | None:
    """根据新闻标题生成卡通风图片。返回图片字节或 None。"""
    top = titles[0] if titles else "今日热点"
    rest = "；".join(titles[1:4])
    prompt = (f"温馨可爱的卡通插画风格（扁平、暖色调、圆润），概括这条新闻：{top}。"
              f"其他相关热点：{rest}。无文字，无水印，竖构图。")
    return generate_image(prompt, ratio=ratio)
