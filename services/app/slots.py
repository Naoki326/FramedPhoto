"""slots.py — 24h 时段编排：决定当前应显示哪种内容。

数据模型：`slot_segments` 是规范的线性 24h 分段（类似磁盘分区）：
    [{start: "00:00", type: "weather"}, {start: "08:00", type: "photo"},
     {start: "19:00", type: "free"},    {start: "21:00", type: "weather"}]
每段从 start 起、到下一段 start 止（最后一段到 24:00），首段必须从 00:00 起，
因此任意时刻恰好命中一段。管理台的「时序控件」读写此结构。

兼容旧配置（.env / 历史 runtime_config 的 slot_weather / slot_photo /
slot_free 三时段写法）：未配置 slot_segments 时自动合成，见 segments()。
自由模块前身为「新闻卡片」时段，slot_news / slot_news_enabled 自动兼容。
"""
from __future__ import annotations

import datetime as dt

from app import runtime_config
from app.config import settings
from app.runtime_config import effective

SLOT_WEATHER = "weather"
SLOT_PHOTO = "photo"
SLOT_FREE = "free"

SLOT_TYPES = {SLOT_WEATHER, SLOT_PHOTO, SLOT_FREE}
MIN_SEGMENT_MIN = 30    # 时序控件允许的最小时段（分钟）
DAY_MIN = 24 * 60


def parse_range(spec: str) -> tuple[int, int] | None:
    """解析 "HH:MM-HH:MM" → (起始分钟, 结束分钟)。结束<起始表示跨天。"""
    try:
        start_s, end_s = spec.split("-")
        sh, sm = start_s.split(":")
        eh, em = end_s.split(":")
        return int(sh) * 60 + int(sm), int(eh) * 60 + int(em)
    except (ValueError, AttributeError):
        return None


def to_minutes(hhmm: str) -> int | None:
    """"HH:MM" → 0..1439；非法返回 None。"""
    try:
        h, m = hhmm.split(":")
        v = int(h) * 60 + int(m)
        return v if 0 <= v < DAY_MIN else None
    except (ValueError, AttributeError):
        return None


def _start_to_minutes(v) -> int | None:
    """分段起始时间：接受分钟整数或 "HH:MM" 字符串（兼容旧存储格式）。"""
    if isinstance(v, int) and not isinstance(v, bool):
        return v if 0 <= v < DAY_MIN else None
    return to_minutes(str(v))


def to_hhmm(minutes: int) -> str:
    minutes %= DAY_MIN
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _legacy_raw() -> list[tuple[int, int, str]]:
    """旧三时段配置 → [(start, end, type)]，跨天段拆成两段。"""
    raw: list[tuple[int, int, str]] = []
    if bool(effective("slot_weather_enabled", settings)):
        r = parse_range(effective("slot_weather", settings) or "")
        if r:
            raw.append((r[0], r[1], SLOT_WEATHER))
    r = parse_range(effective("slot_photo", settings) or "")
    if r:
        raw.append((r[0], r[1], SLOT_PHOTO))
    free_spec = effective("slot_free", settings) or effective("slot_news", settings) or ""
    if (bool(effective("slot_free_enabled", settings))
            or bool(effective("slot_news_enabled", settings))) and free_spec:
        r = parse_range(free_spec)
        if r:
            raw.append((r[0], r[1], SLOT_FREE))
    out: list[tuple[int, int, str]] = []
    for start, end, typ in raw:
        if end <= start:      # 跨天（如 21:00-08:00）→ 拆成两段
            out.append((start, DAY_MIN, typ))
            out.append((0, end, typ))
        else:
            out.append((start, end, typ))
    return sorted(out, key=lambda s: s[0])


def fill_partition(segments: list[tuple[int, int, str]]) -> list[dict]:
    """把 [start, end, type] 列表补成覆盖 0..1440 的线性分段。

    间隙用 photo 填充（photo 是历史保底内容），重叠先到先得。
    """
    out: list[dict] = []
    t = 0
    for start, end, typ in segments:
        start, end = max(0, start), min(DAY_MIN, end)
        if end <= t or start >= DAY_MIN:
            continue
        if start > t:
            out.append({"start": t, "type": SLOT_PHOTO})
        out.append({"start": max(t, start), "type": typ})
        t = max(t, end)
    if t < DAY_MIN:
        out.append({"start": t, "type": SLOT_PHOTO})
    return out


def canonicalize(raw) -> list[dict]:
    """校验并规范化管理台提交的 slot_segments；非法输入抛 ValueError。

    返回 [{start: "HH:MM", type, module}]：按 start 升序；相邻同类型合并
    （free 段须 module 相同才合并）；首段必须从 00:00 起
    （若原首段不在 00:00，则按「跨天」在 00:00 处切开）。
    module 仅对 free 段有意义：固定到某个自由模块（字符串，空/null=随机轮换）。
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("slot_segments 必须为非空列表")
    segs: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("每个时段必须是 {start, type} 对象")
        start = _start_to_minutes(item.get("start"))
        typ = item.get("type")
        if start is None:
            raise ValueError(f"非法起始时间: {item.get('start')!r}")
        if typ not in SLOT_TYPES:
            raise ValueError(f"非法内容类型: {typ!r}")
        module = item.get("module")
        if module is not None and (not isinstance(module, str) or not module.strip()):
            raise ValueError(f"非法固定模块: {module!r}")
        module = module.strip() if isinstance(module, str) else None
        if typ != SLOT_FREE:
            module = None     # 非自由模块时段忽略 module
        segs.append({"start": start, "type": typ, "module": module})

    def _merge_adjacent(items: list[dict]) -> list[dict]:
        out: list[dict] = []
        for s in items:
            prev = out[-1] if out else None
            same = prev and prev["type"] == s["type"] \
                and prev.get("module") == s.get("module")
            if same:
                continue    # 相邻同类型（free 段须固定模块相同）只保留前一个起点
            out.append(dict(s))
        return out

    segs = _merge_adjacent(sorted(segs, key=lambda s: s["start"]))
    if not segs:
        raise ValueError("slot_segments 不能为空")
    if segs[0]["start"] != 0:
        segs.insert(0, {"start": 0, "type": segs[-1]["type"],
                        "module": segs[-1].get("module")})
    segs = _merge_adjacent(segs)    # 切开后可能与原首段同类型，再合并
    out: list[dict] = []
    last = -1
    for s in segs:
        if s["start"] <= last:      # 去重 / 非递增
            continue
        out.append(s)
        last = s["start"]
    if not out:
        raise ValueError("slot_segments 不能为空")
    # 统一输出 "HH:MM" 字符串格式（存储 / API / 前端一致）
    return [{"start": to_hhmm(s["start"]), "type": s["type"],
             "module": s.get("module")} for s in out]


def segments() -> list[dict]:
    """规范时段列表（[{start: "HH:MM", type}]，按 start 升序，首段 00:00 起，末段到 24:00）。

    优先 slot_segments；未配置时由旧三时段配置合成（slot_news 兼容）。
    """
    raw = runtime_config.get("slot_segments")
    if raw is not None:
        return canonicalize(raw)
    return [{"start": to_hhmm(s["start"]), "type": s["type"], "module": None}
            for s in fill_partition(_legacy_raw())]


def current_slot(now: dt.datetime | None = None) -> str:
    """当前分钟 → 命中的内容类型；空配置返回 photo（保底）。"""
    seg = current_segment(now)
    return seg["type"] if seg else SLOT_PHOTO


def current_segment(now: dt.datetime | None = None) -> dict | None:
    """当前分钟 → 命中的完整分段（含 type / module）；无命中返回 None。"""
    now = now or dt.datetime.now()
    minutes = now.hour * 60 + now.minute
    segs = segments()
    for i, seg in enumerate(segs):
        start = _start_to_minutes(seg["start"])
        end = (_start_to_minutes(segs[i + 1]["start"]) if i + 1 < len(segs) else DAY_MIN)
        if start is not None and end is not None and start <= minutes < end:
            return seg
    return None


def segment_start(now: dt.datetime | None = None,
                  typ: str | None = None,
                  module: str | None = None) -> dt.datetime | None:
    """当前命中段（type/module 匹配时）的开始时刻（绝对时间）；无命中返回 None。

    跨午夜连续段：末段与首段 type+module 相同 → 0 点后首段视为前一日的延续，
    开始时刻追溯到前一天末段的 start（如 22:02 历史故事 → 次日 00:00 历史故事，
    段开始仍为前一日 22:02，0 点不换内容）。typ/module 为 None 表示不限制。
    """
    now = now or dt.datetime.now()
    minutes = now.hour * 60 + now.minute
    segs = segments()
    hit_i, hit = None, None
    for i, seg in enumerate(segs):
        start = _start_to_minutes(seg["start"])
        end = (_start_to_minutes(segs[i + 1]["start"]) if i + 1 < len(segs) else DAY_MIN)
        if start is not None and end is not None and start <= minutes < end:
            hit_i, hit = i, seg
            break
    if hit is None:
        return None
    if typ is not None and hit["type"] != typ:
        return None
    if module is not None and hit.get("module") != module:
        return None
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = _start_to_minutes(hit["start"])
    # 跨午夜延续：当前是首段（00:00 起）且末段（前一天最后一刻）与首段同 type+module
    if hit_i == 0 and len(segs) > 1:
        last = segs[-1]
        if last["type"] == hit["type"] and last.get("module") == hit.get("module"):
            last_start = _start_to_minutes(last["start"])
            if last_start is not None:
                return day - dt.timedelta(days=1) + dt.timedelta(minutes=last_start)
    if start is None:
        return None
    return day + dt.timedelta(minutes=start)


def slot_label(slot: str) -> str:
    return {"weather": "天气卡片", "photo": "每日照片", "free": "自由模块"}.get(slot, slot)
