"""runtime_config.py — 管理台可配置项（JSON 存储，即时生效，无需重启）。

优先级：runtime_config.json > .env / 默认值。
管理台「设置」面板读写此文件。

新旧键兼容（别名）与类型化默认值住在本模块内部，消费点不感知：
- 旧键（自由模块前身「新闻卡片」时代）经 ALIASES 解析为新键：读取按
  「新键优先、旧键兜底」的顺序查 runtime_config.json（以键存在与否判定，
  不以真值为准——显式保存的空串/False 也算已配置），兜底才走 .env/默认。
- 每个可调项在 ALLOWED_KEYS 注册处声明类型，读取按声明类型强转返回
  （开关为真布尔、时段串为字符串），消费点不再手写 bool()/or ""。
- 历史遗留键 news_source 无对应新键：原样留在文件里（save 合并不删），
  读取时忽略，不做数据迁移。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "runtime_config.json"

# 可配置项白名单（管理台可改）：键 → 声明类型（读取按此强转返回）。
# list/dict 结构类型不强转，结构校验在各自消费点（如 slots.canonicalize）。
ALLOWED_KEYS: dict[str, type] = {
    "slot_weather": str,
    "slot_photo": str,
    "slot_free": str,
    "slot_weather_enabled": bool,
    "slot_free_enabled": bool,
    "slot_segments": list,   # 时序编排：[{start: "HH:MM", type}]，线性覆盖 24h（见 app.slots）
    "weather_style": str,    # auto=每日轮换 | apple | blue | magazine | classic
    "free_enabled": bool,    # 自由模块时段开关
    "free_rotate": bool,     # 每天轮换启用中的自由模块（false=固定第一个）
    "free_modules": list,    # 自由模块列表 [{name, enabled, prompt, style}]
    "qweather_city": str,    # 城市显示名（空 = IP 自动定位）
    "qweather_location": str,  # 和风 location（空 = IP 自动定位）
    "daily_manual": dict,    # 手动指定今日精选 {path, date}
    "content_push": dict,    # 内容库推送图上屏 {id, date}（当日有效）
}

# 旧键 → 新键别名（自由模块前身「新闻卡片」时段的历史 runtime_config.json）。
# 读取顺序见 _lookup；旧键不经白名单保存（管理台只写新键）。
ALIASES: dict[str, str] = {
    "slot_news": "slot_free",
    "slot_news_enabled": "slot_free_enabled",
}

# 反查表：新键 → 旧键列表（按插入序作兜底顺序）
_LEGACY_KEYS: dict[str, list[str]] = {}
for _old, _new in ALIASES.items():
    _LEGACY_KEYS.setdefault(_new, []).append(_old)
del _old, _new


def load() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _canonical(key: str) -> str:
    """旧键 → 新键（非别名键原样返回）。"""
    return ALIASES.get(key, key)


def _coerce(key: str, value):
    """按注册处声明的类型强转；未注册键与 None 透传（缺省语义由调用方定）。"""
    typ = ALLOWED_KEYS.get(key)
    if typ is None or value is None:
        return value
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if typ is str:
        return value if isinstance(value, str) else str(value)
    return value


def _lookup(rc: dict, key: str):
    """按「新键 → 旧键」顺序查 runtime_config.json，命中即按声明类型强转。"""
    canon = _canonical(key)
    for k in (canon, *_LEGACY_KEYS.get(canon, [])):
        if k in rc:
            return _coerce(canon, rc[k])
    return None


def get(key: str, default=None):
    """读单键（别名透明），未配置返回 default。"""
    value = _lookup(load(), key)
    return default if value is None else value


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
    """运行时优先（新键 → 旧键兜底），其次 settings（.env/默认）。"""
    value = _lookup(load(), key)
    if value is not None:
        return value
    return _coerce(_canonical(key), getattr(default, _canonical(key), None))
