"""slots.py — 24h 时段编排：决定当前应显示哪种内容。

配置（services/.env）：
    SLOT_WEATHER="00:00-10:00"    天气卡片时段
    SLOT_PHOTO="10:00-21:00"      每日照片时段
    SLOT_NEWS="21:00-24:00"       新闻卡通卡片时段
支持跨天时段（如 "21:00-08:00"）。
"""
from __future__ import annotations

import datetime as dt

from app.config import settings

SLOT_WEATHER = "weather"
SLOT_PHOTO = "photo"
SLOT_NEWS = "news"


def parse_range(spec: str) -> tuple[int, int] | None:
    """解析 "HH:MM-HH:MM" → (起始分钟, 结束分钟)。结束<起始表示跨天。"""
    try:
        start_s, end_s = spec.split("-")
        sh, sm = start_s.split(":")
        eh, em = end_s.split(":")
        return int(sh) * 60 + int(sm), int(eh) * 60 + int(em)
    except (ValueError, AttributeError):
        return None


def current_slot(now: dt.datetime | None = None) -> str:
    """当前分钟 → 命中的 slot；无命中返回 photo（保底）。"""
    now = now or dt.datetime.now()
    minutes = now.hour * 60 + now.minute

    slots = []
    if settings.slot_weather_enabled:
        slots.append((SLOT_WEATHER, settings.slot_weather))
    slots.append((SLOT_PHOTO, settings.slot_photo))
    if settings.slot_news_enabled:
        slots.append((SLOT_NEWS, settings.slot_news))

    for name, spec in slots:
        r = parse_range(spec)
        if not r:
            continue
        start, end = r
        if start <= end:
            if start <= minutes < end:
                return name
        else:  # 跨天：start..23:59 + 00:00..end
            if minutes >= start or minutes < end:
                return name
    return SLOT_PHOTO


def slot_label(slot: str) -> str:
    return {"weather": "天气卡片", "photo": "每日照片", "news": "新闻卡片"}.get(slot, slot)
