"""runtime_config.py — 管理台可配置项（JSON 存储，即时生效，无需重启）。

优先级：runtime_config.json > .env / 默认值。
管理台「设置」面板读写此文件。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "runtime_config.json"

# 可配置项白名单（管理台可改）
ALLOWED_KEYS = {
    "slot_weather", "slot_photo", "slot_news",
    "slot_weather_enabled", "slot_news_enabled",
    "news_source",          # 60s | zhipu
    "qweather_city",        # 城市显示名（空 = IP 自动定位）
    "qweather_location",    # 和风 location（空 = IP 自动定位）
}


def load() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get(key: str, default=None):
    return load().get(key, default)


def save(patch: dict) -> dict:
    """合并保存（只允许白名单字段），返回保存后的完整配置。"""
    data = load()
    for k, v in patch.items():
        if k in ALLOWED_KEYS:
            data[k] = v
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return data


def effective(key: str, default):
    """运行时优先，其次 settings（.env/默认）。"""
    rc = load()
    if key in rc:
        return rc[key]
    return getattr(default, key, None)
