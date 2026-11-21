"""ai_secrets.py — API key 的掩码回显与留空不改语义。

管理台配置 LLM / 天气 / NAS 密钥时，页面**不回显明文**：
- GET 侧 `mask(value)` 只给 `sk-1…cd34` 形式（前 6 位 + 末 4 位），
  供页面提示「已配置/未配置」，不足以还原原值；
- PUT 侧 `resolve()` 把页面提交的值解析成「保持原值 / 置空 / 新值」：
  留空（None、空串，或带 "…" 的掩码本身）= 保持原值（配合掩码回显，
  用户不动它就不会误清），
  单个 "-" = 显式置空（runtime_config 落空串，不再回退 .env——一旦经
  管理台配置，管理台就是这几个 key 的唯一权威，能在 UI 里关掉 .env 里的 key），
  其它字符串 = 新值。

密钥本身仍住在 runtime_config.json（管理台可写项的唯一持久层），
本模块只负责这两种语义，避免掩码/留空分支散落在各 router 里。
"""
from __future__ import annotations

from app import runtime_config

#: 显式置空哨兵：页面把该 key 的输入框清空后提交 "-" 表示「不要这个 key」
CLEAR = "-"

#: 掩码保留的字符数（前缀 / 后缀）；前缀段短于 6 位时整体退化为前缀 + …
_PREFIX = 6
_SUFFIX = 4


def mask(value: str | None) -> str:
    """密钥掩码：空值返回空串；短值只给前缀；长值前 6 后 4。"""
    if not value:
        return ""
    text = str(value)
    if len(text) <= _PREFIX + _SUFFIX:
        return text[:_PREFIX] + "…"
    return f"{text[:_PREFIX]}…{text[-_SUFFIX:]}"


def resolve(submitted: str | None, current: str | None) -> str | None:
    """把管理台提交的密钥字段解析为目标值。

    返回 None 表示「不改」（保持 current）；返回 "" 表示清除；
    其它返回值为新值。留空与掩码回显配套：页面预填掩码后用户不编辑，
    提交上来的就是掩码本身（含 "…"），同样按「不改」处理。
    """
    if submitted is None:
        return None
    text = submitted.strip()
    if not text or "…" in text:
        return None
    if text == CLEAR:
        return ""
    return text


def effective(key: str, default) -> str:
    """运行时优先取密钥（runtime_config > settings/.env），未配置返回空串。"""
    value = runtime_config.effective(key, default)
    return "" if value is None else str(value)


def reveal(key: str, current: str | None) -> str:
    """给页面的掩码形态；runtime_config 有覆盖时以它为准（它才是运行时真值）。

    覆盖为空串（用户在此置空过）时返回空掩码——与 status 的就绪判定一致。
    """
    stored = runtime_config.get(key)
    if stored is None:
        return mask(current)
    return mask(str(stored))
