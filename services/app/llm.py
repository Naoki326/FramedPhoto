"""llm.py — 文本 LLM 与文生图接入的「当前生效值」解析。

配置面按**接口形态**描述，不绑定任何厂商：管理台只让用户选 provider
（文本 LLM：OpenAI 兼容 / Anthropic 兼容；文生图：OpenAI 兼容图片接口 /
签名式异步接口）+ 地址 + 凭据，换厂家 = 换地址与 key，不改代码。

管理台「AI 模型」页写入 runtime_config.json，覆盖 services/.env 与默认值；
消费点（自由模块 / 日历寄语 / 天气卡片设计微调 / 两条出图链路）统一经
本模块取值，改完即时生效，无需重启服务。

密钥不在这里存储或缓存：每次调用现读现用。
"""
from __future__ import annotations

import re

from app.config import settings
from app.runtime_config import effective

#: 文本 LLM 端点形态
TEXT_API_OPENAI = "openai"          # OpenAI 兼容：<base>/chat/completions
TEXT_API_ANTHROPIC = "anthropic"    # Anthropic 兼容：<base>/v1/messages

#: 文生图端点形态
IMAGE_API_CLOUDFLARE = "cloudflare"
IMAGE_API_OPENAI = "openai"          # OpenAI 兼容图片接口（同步返回图片）
IMAGE_API_SIGNED_ASYNC = "signed_async"   # 签名式异步任务接口（提交 + 轮询取图）

DEFAULT_TEXT_BASE = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_TEXT_MODEL = "glm-4-flash"


def _str(key: str, default: str = "") -> str:
    value = effective(key, settings)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _openai_chat_url(base: str) -> str:
    """OpenAI 兼容 base → chat/completions 完整地址（已是完整地址则原样返回）。"""
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


# ═══════════════════════ 文本 LLM ═══════════════════════

def text_api() -> str:
    """文本 LLM 端点形态（openai | anthropic）。"""
    api = _str("text_api", TEXT_API_OPENAI).lower()
    return api if api in (TEXT_API_OPENAI, TEXT_API_ANTHROPIC) else TEXT_API_OPENAI


def text_base_url() -> str:
    """文本 LLM base URL（管理台可改；空则用默认端点）。"""
    return _str("text_base_url", DEFAULT_TEXT_BASE)


def text_key() -> str:
    """文本 LLM API key；未配置返回空串。"""
    return _str("text_api_key")


def text_model() -> str:
    """文本 LLM 模型名。"""
    return _str("text_model", DEFAULT_TEXT_MODEL)


def text_chat_url() -> str:
    """当前配置下的完整对话端点地址。"""
    base = text_base_url()
    if text_api() == TEXT_API_ANTHROPIC:
        base = base.rstrip("/")
        return base if base.endswith("/messages") else f"{base}/v1/messages"
    return _openai_chat_url(base)


def text_url_and_model() -> tuple[str, str]:
    """(完整端点地址, 模型) —— 自由模块 / 日历 / 天气设计三处的共同取用口。"""
    return text_chat_url(), text_model()


def text_readiness() -> dict:
    return {"ready": bool(text_key()), "model": text_model(), "api": text_api()}


# ═══════════════════════ 文生图 ═══════════════════════

def image_api() -> str:
    """文生图端点形态（openai | signed_async | cloudflare）。"""
    api = _str("image_api", IMAGE_API_OPENAI).lower()
    return api if api in (IMAGE_API_OPENAI, IMAGE_API_SIGNED_ASYNC, IMAGE_API_CLOUDFLARE) else IMAGE_API_OPENAI


def image_conf() -> tuple[str, str, str]:
    """OpenAI 兼容图片接口 (url, key, model)；未配置齐返回三项空串。"""
    return (_str("image_api_url"), _str("image_api_key"), _str("image_model"))


def image_signed_conf() -> tuple[str, str]:
    """签名式异步接口 (access_key, secret_key)；未配置齐返回两项空串。"""
    return _str("image_signed_ak"), _str("image_signed_sk")


def valid_cloudflare_account_id(value: str) -> bool:
    return re.fullmatch(r"[0-9a-fA-F]{32}", value) is not None


def cloudflare_conf() -> tuple[str, str]:
    return _str("cloudflare_account_id"), _str("cloudflare_api_token")


def cloudflare_ready() -> bool:
    account, token = cloudflare_conf()
    return bool(valid_cloudflare_account_id(account) and token)


def image_ratio() -> str:
    """出图比例（相框横放观看，默认 4:3）。"""
    return _str("image_ratio", "4:3")


def image_readiness() -> dict:
    """文生图就绪状态：按当前 provider 判定（管理台状态提示用）。"""
    api = image_api()
    if api == IMAGE_API_CLOUDFLARE:
        ready = cloudflare_ready()
    elif api == IMAGE_API_SIGNED_ASYNC:
        ak, sk = image_signed_conf()
        ready = bool(ak and sk)
    else:
        url, key, model = image_conf()
        ready = bool(url and key and model)
    return {"ready": ready, "api": api, "ratio": image_ratio(),
            "cloudflare_ready": cloudflare_ready(),
            "signed_ready": all(image_signed_conf()),
            "openai_ready": all(image_conf())}
