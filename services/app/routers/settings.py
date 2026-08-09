"""settings.py — 管理台设置（时段编排 / 天气 / 自由模块）。

- GET  ""  返回当前生效配置 + 状态（IP 定位城市、当前时段、今日自由模块）
- PUT  ""  保存可配置项（runtime_config.json，即时生效）
"""
from typing import Any

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import runtime_config, slots
from app.config import settings
from app.free_module import default_modules, enabled_modules, pick_module

router = APIRouter()


class SettingsBody(BaseModel):
    slot_weather: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    slot_photo: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    slot_free: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    slot_weather_enabled: bool | None = None
    slot_free_enabled: bool | None = None
    slot_segments: list[dict[str, Any]] | None = None
    free_enabled: bool | None = None
    free_rotate: bool | None = None
    free_modules: list[dict[str, Any]] | None = None
    qweather_city: str | None = None
    qweather_location: str | None = None
    weather_style: str | None = Field(default=None, pattern=r"^(auto|apple|blue|magazine|classic)$")


_QWEATHER_LOC_PATTERN = re.compile(
    r"^(?:\d{1,3}\.\d{1,3},\d{1,3}\.\d{1,3}|\d{3,9}|[A-Za-z]{2,10})$|^\d{6}$"
)


def _validate_location(loc: str) -> None:
    """校验和风 location：经纬度 / 城市ID / 拼音（见 _QWEATHER_LOC_PATTERN）。非法则 400。"""
    if not _QWEATHER_LOC_PATTERN.match(loc.strip()):
        raise HTTPException(
            status_code=400,
            detail="和风 location 需为经纬度(如 121.49,31.40)、城市ID(如 101020300)或拼音(如 shanghai)",
        )


@router.get("")
async def get_settings():
    rc = runtime_config.load()
    from app.weather_card import ip_location
    loc = ip_location() or {}
    today_mod = pick_module()
    return {
        "config": {
            "slot_weather": runtime_config.effective("slot_weather", settings),
            "slot_photo": runtime_config.effective("slot_photo", settings),
            "slot_free": (runtime_config.effective("slot_free", settings)
                          or runtime_config.effective("slot_news", settings)),
            "slot_weather_enabled": runtime_config.effective("slot_weather_enabled", settings),
            "slot_free_enabled": bool(runtime_config.effective("slot_free_enabled", settings)
                                      or runtime_config.effective("slot_news_enabled", settings)),
            "slot_segments": slots.segments(),
            "free_enabled": runtime_config.effective("free_enabled", settings),
            "free_rotate": runtime_config.effective("free_rotate", settings),
            "free_modules": runtime_config.get("free_modules") or default_modules(),
            "qweather_city": runtime_config.effective("qweather_city", settings),
            "qweather_location": runtime_config.effective("qweather_location", settings),
            "weather_style": runtime_config.effective("weather_style", settings),
        },
        "runtime_overrides": list(rc.keys()),
        "status": {
            "ip_city": loc.get("city", ""),
            "current_slot": slots.current_slot(),
            "weather_ready": bool(settings.qweather_key),
            "weather_style_today": _today_style(),
            "free_today": today_mod["name"] if today_mod else "",
            "free_llm_ready": bool(settings.zhipu_api_key),
        },
    }


def _today_style() -> str:
    from app.weather_card import pick_style, STYLE_LABELS
    s = pick_style()
    return f"{s} · {STYLE_LABELS.get(s, s)}"


def _clear_weather_cache():
    """删除天气卡片缓存（数据 + 当日设计），强制下次渲染重新拉取。"""
    from app import weather_card
    for f in weather_card.CACHE_DIR.glob("weather_*.fps6"):
        try:
            f.unlink()
        except OSError:
            pass
    for f in weather_card.CACHE_DIR.glob("design_*.json"):
        try:
            f.unlink()
        except OSError:
            pass


@router.put("")
def save_settings(body: SettingsBody):
    patch: dict[str, Any] = {}
    changed_weather = False
    for k, v in body.model_dump().items():
        if v is None:
            continue
        if k == "slot_segments":
            try:
                patch[k] = slots.canonicalize(v)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        elif k == "qweather_location":
            _validate_location(v)
            patch[k] = v.strip()
            changed_weather = True
        elif k == "qweather_city":
            patch[k] = v.strip()
            changed_weather = True
        else:
            patch[k] = v
    saved = runtime_config.save(patch)
    # 天气相关设置变化 → 清缓存，让天气时段立即用新位置渲染
    if changed_weather:
        _clear_weather_cache()
    return {"ok": True, "saved": {k: saved[k] for k in patch if k in saved}}
