"""settings.py — 管理台设置（时段编排 / 天气 / 新闻源）。

- GET  ""  返回当前生效配置 + 状态（IP 定位城市、当前时段）
- PUT  ""  保存可配置项（runtime_config.json，即时生效）
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import runtime_config, slots
from app.config import settings

router = APIRouter()


class SettingsBody(BaseModel):
    slot_weather: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    slot_photo: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    slot_news: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    slot_weather_enabled: bool | None = None
    slot_news_enabled: bool | None = None
    news_source: str | None = Field(default=None, pattern=r"^(60s|zhipu)$")
    qweather_city: str | None = None
    qweather_location: str | None = None


@router.get("")
async def get_settings():
    rc = runtime_config.load()
    from app.weather_card import ip_location
    loc = ip_location() or {}
    return {
        "config": {
            "slot_weather": runtime_config.effective("slot_weather", settings),
            "slot_photo": runtime_config.effective("slot_photo", settings),
            "slot_news": runtime_config.effective("slot_news", settings),
            "slot_weather_enabled": runtime_config.effective("slot_weather_enabled", settings),
            "slot_news_enabled": runtime_config.effective("slot_news_enabled", settings),
            "news_source": runtime_config.effective("news_source", settings),
            "qweather_city": runtime_config.effective("qweather_city", settings),
            "qweather_location": runtime_config.effective("qweather_location", settings),
        },
        "runtime_overrides": list(rc.keys()),
        "status": {
            "ip_city": loc.get("city", ""),
            "current_slot": slots.current_slot(),
            "weather_ready": bool(settings.qweather_key),
            "news_source_active": runtime_config.effective("news_source", settings) or "60s",
        },
    }


@router.put("")
async def save_settings(body: SettingsBody):
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    saved = runtime_config.save(patch)
    return {"ok": True, "saved": {k: saved[k] for k in patch if k in saved}}
