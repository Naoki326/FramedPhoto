"""ai.py — 管理台「AI 模型」页：LLM / 文生图 / 天气与 NAS 接入配置。

- GET  ""      返回当前生效配置（密钥掩码）+ 各组就绪状态
- PUT  ""      保存配置（runtime_config.json，即时生效；密钥留空即不改）
- POST "/test" 用当前生效配置调一次真实请求，验证连通性
- GET  "/calendar-quote"  当日寄语（可选 force 重生成）

配置面按**接口形态**描述、不绑厂家：文本 LLM 选 openai/anthropic 兼容 +
base_url + key + model；文生图选 openai 兼容 / 签名式异步 + 对应凭据。
换厂家 = 换 provider 与地址，不改代码。

密钥语义见 app/ai_secrets：GET 只给 `sk-1…cd34` 掩码，PUT 留空保持原值、
"-" 置空（不再回退 .env）。因此前端可放心整表提交。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import ai_secrets, calendar_card, llm, runtime_config
from app.config import settings
from app.runtime_config import SECRET_KEYS, effective

router = APIRouter()

#: 本页可改的非密钥字段（类型/白名单住在 runtime_config）
_TEXT_KEYS = (
    "vlm_provider", "vlm_api_mode", "vlm_api_url", "vlm_model", "vlm_proxy",
    "anthropic_model", "anthropic_base_url",
    "text_api", "text_base_url", "text_model",
    "image_api", "image_api_url", "image_model", "image_ratio", "cloudflare_account_id",
    "weather_host", "nas_ssh_host", "nas_ssh_user", "nas_photo_dir", "nas_rsync_path",
)
_BOOL_KEYS = ("vlm_enabled",)
_INT_KEYS = ("vlm_timeout", "nas_ssh_port", "nas_rsync_bwlimit")
#: 密钥字段（掩码回显 / 留空不改 / "-" 置空）
_SECRET_FIELDS = tuple(sorted(SECRET_KEYS))

_PROVIDERS = ("auto", "openai", "anthropic", "disabled")
_API_MODES = ("chat", "responses")
_TEXT_APIS = (llm.TEXT_API_OPENAI, llm.TEXT_API_ANTHROPIC)
_IMAGE_APIS = (llm.IMAGE_API_OPENAI, llm.IMAGE_API_SIGNED_ASYNC, llm.IMAGE_API_CLOUDFLARE)
_IMAGE_RATIOS = ("4:3", "3:4", "16:9", "9:16")


class AiBody(BaseModel):
    # 非密钥字段：None = 不改（前端整表提交时用 null 表示未编辑）
    vlm_enabled: bool | None = None
    vlm_provider: str | None = None
    vlm_api_mode: str | None = None
    vlm_api_url: str | None = None
    vlm_model: str | None = None
    vlm_timeout: int | None = None
    vlm_proxy: str | None = None
    anthropic_model: str | None = None
    anthropic_base_url: str | None = None
    text_api: str | None = None
    text_base_url: str | None = None
    text_model: str | None = None
    cloudflare_account_id: str | None = None
    cloudflare_api_token: str | None = None
    image_api: str | None = None
    image_api_url: str | None = None
    image_model: str | None = None
    image_ratio: str | None = None
    weather_host: str | None = None
    nas_ssh_host: str | None = None
    nas_ssh_user: str | None = None
    nas_ssh_port: int | None = None
    nas_photo_dir: str | None = None
    nas_rsync_path: str | None = None
    nas_rsync_bwlimit: int | None = None
    # 密钥字段："" = 不改，"-" = 置空，其它 = 新值
    vlm_api_key: str | None = None
    anthropic_api_key: str | None = None
    text_api_key: str | None = None
    image_api_key: str | None = None
    image_signed_ak: str | None = None
    image_signed_sk: str | None = None
    weather_key: str | None = None


def _effective_str(key: str) -> str:
    """当前生效值（runtime_config > .env/默认），统一成字符串返回。"""
    value = effective(key, settings)
    return "" if value is None else str(value)


def _config_payload() -> dict[str, Any]:
    """生效配置：非密钥给明文现值，密钥给掩码（页面据此提示是否已配置）。"""
    out: dict[str, Any] = {k: _effective_str(k) for k in _TEXT_KEYS}
    out["vlm_enabled"] = bool(effective("vlm_enabled", settings))
    for k in _INT_KEYS:
        out[k] = int(effective(k, settings) or 0)
    for k in _SECRET_FIELDS:
        out[k] = ai_secrets.reveal(k, getattr(settings, k, ""))
        out[f"{k}_set"] = bool(_effective_str(k))
    return out


def _status_payload() -> dict[str, Any]:
    """各组就绪状态（页面顶部状态条 + 设置页提示用）。"""
    from app.analyzer import build_client
    provider = _effective_str("vlm_provider").strip().lower()
    text = llm.text_readiness()
    image = llm.image_readiness()
    return {
        "vlm_provider": provider,
        "vlm_ready": build_client() is not None,
        "vlm_disabled": provider == "disabled" or not bool(effective("vlm_enabled", settings)),
        "text_ready": text["ready"],
        "text_model": text["model"],
        "text_api": text["api"],
        "image_ready": image["ready"],
        "cloudflare_ready": image["cloudflare_ready"],
        "image_api": image["api"],
        "image_ratio": image["ratio"],
        "image_openai_ready": image["openai_ready"],
        "image_signed_ready": image["signed_ready"],
        "weather_ready": bool(_effective_str("weather_key")),
        "nas_ready": bool(_effective_str("nas_ssh_host") and _effective_str("nas_ssh_user")
                          and _effective_str("nas_photo_dir")),
        "runtime_overrides": sorted(k for k in runtime_config.load() if k in runtime_config.ALLOWED_KEYS),
    }


@router.get("/status")
async def get_ai_status():
    return _status_payload()


@router.get("")
async def get_ai_config():
    return {"config": _config_payload(), "status": _status_payload()}


def _validate(patch: dict[str, Any]) -> None:
    """枚举类字段校验（避免管理台写入无法解释的值）。"""
    if "vlm_provider" in patch and patch["vlm_provider"].strip().lower() not in _PROVIDERS:
        raise HTTPException(400, f"provider 需为 {'/'.join(_PROVIDERS)}")
    if "vlm_api_mode" in patch and patch["vlm_api_mode"].strip().lower() not in _API_MODES:
        raise HTTPException(400, f"api_mode 需为 {'/'.join(_API_MODES)}")
    if "text_api" in patch and patch["text_api"].strip().lower() not in _TEXT_APIS:
        raise HTTPException(400, f"文本接口类型需为 {'/'.join(_TEXT_APIS)}")
    if "image_api" in patch and patch["image_api"].strip().lower() not in _IMAGE_APIS:
        raise HTTPException(400, f"文生图接口类型需为 {'/'.join(_IMAGE_APIS)}")
    if (patch.get("cloudflare_account_id")
            and not llm.valid_cloudflare_account_id(patch["cloudflare_account_id"])):
        raise HTTPException(400, "Cloudflare account id 需为 32 位十六进制字符串（或留空清除）")
    if "image_ratio" in patch and patch["image_ratio"].strip() not in _IMAGE_RATIOS:
        raise HTTPException(400, f"出图比例需为 {'/'.join(_IMAGE_RATIOS)}")
    if "vlm_timeout" in patch and patch["vlm_timeout"] <= 0:
        raise HTTPException(400, "VLM 超时需为正整数秒")


@router.put("")
def save_ai_config(body: AiBody):
    raw = body.model_dump()
    patch: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _SECRET_FIELDS:
            # 密钥走「留空不改 / "-" 置空 / 其它为新值」
            resolved = ai_secrets.resolve(value, _effective_str(key))
            if resolved is not None:
                patch[key] = resolved
            continue
        if value is None:
            continue
        patch[key] = value.strip() if isinstance(value, str) else value
    _validate(patch)
    runtime_config.save(patch)
    # 天气端点 / key 变了 → 清天气缓存，下一时段立即用新配置渲染
    if {"weather_key", "weather_host"} & patch.keys():
        from app.routers.settings import _clear_weather_cache
        _clear_weather_cache()
    return {"ok": True, "saved": {k: ("***" if k in _SECRET_FIELDS else v)
                                  for k, v in patch.items()},
            "status": _status_payload()}


class TestBody(BaseModel):
    """连通性测试目标：vlm（照片分析视觉模型）| text（文本 LLM）| image（文生图）。"""

    target: str = "vlm"


def _test_vlm() -> dict:
    """向 VLM 发一张小图，验证端点/密钥/代理可用（真实调用）。"""
    import io

    from PIL import Image

    from app.analyzer import build_client
    client = build_client()
    if client is None:
        return {"ok": False, "detail": "VLM 未配置（provider / api_url / api_key 缺失）"}
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (255, 255, 255)).save(buf, format="JPEG")
    raw = buf.getvalue()
    started = time.time()
    try:
        if hasattr(client, "generate_caption"):
            text = client.generate_caption(raw)
        else:
            text = str(client.analyze(raw))
        return {"ok": True, "detail": f"{client.__class__.__name__} 响应正常",
                "sample": (text or "").strip()[:60], "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001  连通性测试：任何异常都作为结果返回
        return {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"[:300]}


def _test_text() -> dict:
    """向文本 LLM 发一次最小 chat 请求，验证 base_url / key / 模型名可用。"""
    import requests
    key = llm.text_key()
    if not key:
        return {"ok": False, "detail": "文本 LLM key 未配置"}
    url, model = llm.text_url_and_model()
    started = time.time()
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": "回复两个字：正常"}],
                  "max_tokens": 16},
            timeout=20,
        )
        if r.status_code != 200:
            return {"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
        content = r.json()["choices"][0]["message"]["content"]
        return {"ok": True, "detail": f"{model} 响应正常",
                "sample": content.strip()[:60], "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"[:300]}


def _test_image() -> dict:
    """文生图就绪性检查：不实际出图（按次计费），只报告会走哪条链路。"""
    image = llm.image_readiness()
    if image["api"] == llm.IMAGE_API_CLOUDFLARE:
        return {"ok": image["cloudflare_ready"],
                "detail": ("Cloudflare 配置已就绪" if image["cloudflare_ready"] else
                           "Cloudflare 未配置（需有效的 account id 和 API token）")
                          + " — 仅检查配置，未实际生成、不消费额度；固定模型默认尺寸，不应用比例设置"}
    if image["api"] == llm.IMAGE_API_SIGNED_ASYNC:
        if image["signed_ready"]:
            return {"ok": True, "detail": f"签名式异步接口已配置（比例 {image['ratio']}）"
                                          f" — 实际出图请在「当前显示」页点重新生成"}
        if image["openai_ready"]:
            return {"ok": True, "detail": "签名式凭据不全，将回退 OpenAI 兼容图片接口"}
        return {"ok": False, "detail": "文生图未配置（签名式 AK/SK 或 OpenAI 兼容 URL+KEY+MODEL）"}
    url, key, model = llm.image_conf()
    if url and key and model:
        return {"ok": True, "detail": f"OpenAI 兼容图片接口已配置（{model} @ {url}）"}
    if image["signed_ready"]:
        return {"ok": True, "detail": "OpenAI 兼容三项不全，将回退签名式异步接口"}
    return {"ok": False, "detail": "文生图未配置（OpenAI 兼容 URL+KEY+MODEL 或签名式 AK/SK）"}


@router.post("/test")
def test_ai_config(body: TestBody):
    target = (body.target or "vlm").strip().lower()
    if target == "vlm":
        return _test_vlm()
    if target in ("text", "llm"):
        return _test_text()
    if target == "image":
        return _test_image()
    raise HTTPException(400, "target 需为 vlm | text | image")


@router.get("/calendar-quote")
async def calendar_quote(force: bool = False):
    """当日家庭寄语（AI 页调试用）；force=true 忽略缓存重生成。"""
    import datetime as dt
    if force:
        try:
            calendar_card._quote_file(dt.date.today()).unlink()
        except OSError:
            pass
    return {"quote": calendar_card.daily_quote(), "model": llm.text_model(),
            "llm_ready": bool(llm.text_key())}
