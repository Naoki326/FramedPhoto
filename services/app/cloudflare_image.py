"""Fixed Workers AI FLUX adapter; no alternate or paid-provider fallback.

Official schema checked at https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/
uses prompt and steps (not num_steps), with no width/height fields. Use the
model's default dimensions; image_ratio is intentionally not sent.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import time
import warnings

import requests
from PIL import Image

from app import llm

logger = logging.getLogger(__name__)
MODEL = "@cf/black-forest-labs/flux-1-schnell"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_SIDE = 4096
MAX_PIXELS = 4096 * 4096


def generate_image(prompt: str) -> bytes | None:
    """Generate once; config/network/quota/invalid image failures return None.

    HTTP connect/read limits are 5/60 seconds; streamed responses also have a
    65-second elapsed-time budget and a 12 MiB body cap. Never log credentials,
    server payloads, exception messages, headers, or prompts.
    """
    account, token = llm.cloudflare_conf()
    if not llm.valid_cloudflare_account_id(account) or not token:
        return None
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2048:
        return None
    response = None
    try:
        started = time.monotonic()
        response = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"prompt": prompt, "steps": 4},
            timeout=(5, 60), allow_redirects=False, stream=True,
        )
        if response.status_code != 200:
            logger.warning("Cloudflare image request failed (HTTP %d)", response.status_code)
            return None
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if time.monotonic() - started > 65 or len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                return None
            body.extend(chunk)
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get("success") is False:
            return None
        result = payload.get("result")
        encoded = result.get("image") if isinstance(result, dict) else None
        if not isinstance(encoded, str) or not encoded or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            return None
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if (image.format not in ("PNG", "JPEG") or
                        not 0 < width <= MAX_SIDE or not 0 < height <= MAX_SIDE or
                        width * height > MAX_PIXELS):
                    return None
                image.verify()
            # verify() alone does not fully decode JPEG pixel data.
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
        return raw
    except Exception as exc:  # External/malformed data must degrade safely.
        logger.warning("Cloudflare image failed: %s", type(exc).__name__)
        return None
    finally:
        if response is not None:
            response.close()
