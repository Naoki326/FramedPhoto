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
    "slot_weather", "slot_photo", "slot_free",
    "slot_weather_enabled", "slot_free_enabled",
    "slot_segments",        # 时序编排：[{start: "HH:MM", type}]，线性覆盖 24h（见 app.slots）
    "weather_style",        # auto=每日轮换 | apple | blue | magazine | classic
    "free_enabled",         # 自由模块时段开关
    "free_rotate",          # 每天轮换启用中的自由模块（false=固定第一个）
    "free_modules",         # 自由模块列表 [{name, enabled, prompt, style}]
    "qweather_city",        # 城市显示名（空 = IP 自动定位）
    "qweather_location",    # 和风 location（空 = IP 自动定位）
    "daily_manual",         # 手动指定今日精选 {path, date}
    "content_push",        # 内容库推送图上屏 {id, date}（当日有效）
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
