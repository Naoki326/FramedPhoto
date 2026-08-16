"""weather_card.py — 天气 + 日历卡片（多风格，每日轮换）。

数据源：和风天气（devapi.qweather.com，QWEATHER_KEY）。
渲染：PIL 排版（中文用 find_cjk_font）→ FPS6（epd_image.prepare_image）。
无 key / 请求失败时：render_weather_slot() 返回 None，调用方回退其他时段内容。

风格机制：
  - 风格池 STYLES（apple / blue / magazine / classic），按日期 toordinal 每日轮换，
    同一天内（含 1 小时数据刷新缓存）风格固定，跨天自动换款。
  - 管理台可用 weather_style 固定某一款（auto = 每日轮换）。
  - LLM 每日微调（可选）：配置了 zhipu_api_key 时，每天由 LLM 从受限枚举中选择
    「强调色 / 标题条深浅 / 装饰符号 / 是否显示农历」，生成 JSON 缓存到磁盘；
    无 key 或 LLM 失败 → 默认设计参数，不影响渲染。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
from lunar_python import Solar

from app.config import settings
from app.epd_image import find_cjk_font, prepare_image

logger = logging.getLogger(__name__)

QWEATHER_BASE = "https://{host}/v7/weather"

# IP 定位缓存（内存 + 磁盘双缓存，重启不丢；IP 不常变，TTL 24h）
_IP_LOC_CACHE: dict | None = None
_IP_LOC_TS: float = 0
_IP_LOC_TTL = 24 * 3600

# 渲染缓存目录与 IP 定位缓存文件：config 公开设置（WEATHER_CACHE_DIR /
# IP_LOC_CACHE_FILE 可覆盖，默认与原私有路径一致：ip_loc.json 随天气缓存目录）
CACHE_DIR = Path(settings.weather_cache_dir)
CACHE_DIR.mkdir(exist_ok=True)
_IP_LOC_FILE = Path(settings.ip_loc_cache_file)


def _ip_loc_disk() -> tuple[dict | None, float]:
    """磁盘缓存 → (loc, mtime)。无/损坏返回 (None, 0)。"""
    try:
        if not _IP_LOC_FILE.exists():
            return None, 0
        raw = json.loads(_IP_LOC_FILE.read_text(encoding="utf-8"))
        return raw.get("loc"), raw.get("ts", 0)
    except (OSError, json.JSONDecodeError):
        return None, 0


def ip_location() -> dict | None:
    """IP 定位 → {city, lat, lon}。失败返回 None（内存+磁盘缓存 24h，过期重试失败可回退）。"""
    global _IP_LOC_CACHE, _IP_LOC_TS
    now = dt.datetime.now().timestamp()
    if _IP_LOC_CACHE and (now - _IP_LOC_TS) < _IP_LOC_TTL:
        return _IP_LOC_CACHE
    loc, ts = _ip_loc_disk()
    if loc and (now - ts) < _IP_LOC_TTL:
        _IP_LOC_CACHE, _IP_LOC_TS = loc, ts
        return loc
    try:
        r = requests.get("http://ip-api.com/json/?lang=zh-CN", timeout=8)
        d = r.json()
        if d.get("status") != "success":
            return loc if loc else None
        city = f"{d.get('regionName','')} {d.get('city','')}".strip()
        new_loc = {"city": city or "未知", "lat": d.get("lat"), "lon": d.get("lon")}
        _IP_LOC_CACHE, _IP_LOC_TS = new_loc, now
        try:
            _IP_LOC_FILE.write_text(json.dumps({"loc": new_loc, "ts": now}, ensure_ascii=False),
                                    encoding="utf-8")
        except OSError:
            pass
        return new_loc
    except Exception as exc:  # noqa: BLE001
        logger.warning("ip_location failed: %s", exc.__class__.__name__)
        return loc if loc else None


def resolve_location() -> tuple[str, str]:
    """返回 (和风 location 参数, 城市显示名)。运行时配置 > .env > IP 自动定位。"""
    from app.runtime_config import effective
    loc_id = effective("qweather_location", settings) or ""
    city = effective("qweather_city", settings) or ""
    # 用户显式设置过城市（qweather_city 或 location），就尊重它；
    # 完全没设过（location 还是默认北京 ID 且 city 为空）才走 IP 自动定位。
    if not loc_id:
        if city:
            return loc_id, city   # 有城市名没 location → 返回空 loc，让调用方按城市名查
        ip = ip_location()
        if ip and ip.get("lon") is not None:
            return f"{ip['lon']},{ip['lat']}", ip.get("city", "") or city
    return loc_id, city

# 和风天气现象代码 → (中文, 图标风格)
WMO = {
    "100": ("晴", "sun"), "101": ("多云", "cloud"), "102": ("少云", "cloud"),
    "103": ("晴间多云", "cloud"), "104": ("阴", "cloud"),
    "300": ("阵雨", "rain"), "301": ("强阵雨", "rain"), "302": ("雷阵雨", "storm"),
    "303": ("强雷阵雨", "storm"), "304": ("雷阵雨伴有冰雹", "storm"),
    "305": ("小雨", "rain"), "306": ("中雨", "rain"), "307": ("大雨", "rain"),
    "308": ("极端降雨", "rain"), "309": ("毛毛雨", "rain"), "310": ("暴雨", "rain"),
    "311": ("大暴雨", "rain"), "312": ("特大暴雨", "rain"), "313": ("冻雨", "rain"),
    "400": ("小雪", "snow"), "401": ("中雪", "snow"), "402": ("大雪", "snow"),
    "403": ("暴雪", "snow"), "404": ("雨夹雪", "snow"), "500": ("薄雾", "fog"),
    "501": ("雾", "fog"), "502": ("霾", "fog"), "503": ("扬沙", "fog"),
    "504": ("浮尘", "fog"), "507": ("强沙尘暴", "fog"), "508": ("沙尘暴", "fog"),
}


def _norm_text(text: str) -> str:
    """去掉和风返回的异常控制符（如 \\u2028）。"""
    return "".join(c for c in (text or "") if c.isprintable() or c == " ")


# ═══════════════════════ 日历信息（节日/节气） ═══════════════════════

# 重要公历/农历节日（过滤掉观莲节等小众节日）
_MAJOR_FESTIVALS = {
    "元旦", "春节", "元宵", "情人节", "妇女节", "植树节", "清明", "劳动节", "青年节",
    "母亲节", "儿童节", "端午", "父亲节", "建党节", "建军节", "七夕", "中元",
    "教师节", "中秋", "国庆", "重阳", "万圣节", "感恩节", "平安夜", "圣诞",
    "腊八", "除夕",
}


# 公历节日（lunar_python 的 getOtherFestivals 只含传统节日，公历的需硬编码）
_SOLAR_FESTIVALS = {
    (1, 1): "元旦", (2, 14): "情人节", (3, 8): "妇女节", (3, 12): "植树节",
    (5, 1): "劳动节", (5, 4): "青年节", (6, 1): "儿童节", (7, 1): "建党节",
    (8, 1): "建军节", (9, 10): "教师节", (10, 1): "国庆节", (12, 24): "平安夜",
    (12, 25): "圣诞",
}


def _day_mark(d: dt.date) -> str:
    """当天标注：节气优先，其次公历节日，再传统节日；无返回空串。"""
    sol = _SOLAR_FESTIVALS.get((d.month, d.day))
    if sol:
        return sol
    try:
        ll = Solar.fromYmd(d.year, d.month, d.day).getLunar()
    except Exception:  # noqa: BLE001
        return ""
    jq = ll.getJieQi()
    if jq:
        return jq
    for f in ll.getFestivals() + ll.getOtherFestivals():
        core = f[:-1] if f.endswith("节") else f
        if f in _MAJOR_FESTIVALS or core in _MAJOR_FESTIVALS:
            return f if f in _MAJOR_FESTIVALS else core
    return ""


def _calendar_info(today: dt.date, days_ahead: int = 10, max_marks: int = 2) -> tuple[str, str]:
    """→ (今日标注, 未来预告串)。预告如 "立秋 8/7 · 七夕 8/19"。"""
    today_mark = _day_mark(today)
    soon: list[str] = []
    for i in range(1, days_ahead + 1):
        d = today + dt.timedelta(days=i)
        m = _day_mark(d)
        if m:
            soon.append(f"{m} {d.month}/{d.day}")
        if len(soon) >= max_marks:
            break
    return today_mark, " · ".join(soon)


# ═══════════════════════ 风格池 ═══════════════════════

# 风格 → 中文名
STYLE_LABELS = {
    "apple": "Apple 天气风",
    "blue": "多彩卡片",
    "magazine": "杂志排版",
    "classic": "经典微调",
}
STYLES = tuple(STYLE_LABELS)


def style_override() -> str:
    """管理台固定风格（runtime_config weather_style，auto = 轮换）。"""
    from app.runtime_config import effective
    val = str(effective("weather_style", settings) or "auto").strip()
    return val if val in STYLES else "auto"


def pick_style(today: dt.date | None = None) -> str:
    """每日风格：固定风格 > 按日期轮换（同一天稳定）。"""
    today = today or dt.date.today()
    ov = style_override()
    if ov in STYLES:
        return ov
    return STYLES[today.toordinal() % len(STYLES)]


# ═══════════════════════ LLM 每日设计参数（可选） ═══════════════════════

DEFAULT_DESIGN = {"accent": "black", "title_band": "light", "deco": "none"}

_DESIGN_ACCENTS = ("black", "red", "blue", "green", "yellow")
_DESIGN_DECOS = ("none", "sun", "star", "snow", "wave")


def _llm_design_json(style: str, summary: str) -> dict | None:
    """调智谱 GLM（OpenAI 兼容端点）生成受限设计参数 JSON。失败返回 None。"""
    key = settings.zhipu_api_key
    if not key:
        return None
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    system = (
        "你是墨水屏天气卡片的设计师。墨水屏只有 6 色：黑/白/黄/红/蓝/绿，"
        "设计必须高对比、扁平、克制。根据给定的天气与风格，从受限选项中挑选设计参数，"
        '只输出 JSON，不要任何其他文字或解释。JSON 形如：'
        '{"accent": "black", "title_band": "light", "deco": "none"}'
    )
    user = (
        f"今天的天气：{summary}。当前卡片风格：{STYLE_LABELS.get(style, style)}。\n"
        "可选值：\n"
        '- accent（强调色，用于温度数字/图标点缀）："black"|"red"|"blue"|"green"|"yellow"，选与天气氛围相配的\n'
        '- title_band（顶部日期条深浅）："light"|"dark"\n'
        '- deco（角落装饰符号）："none"|"sun"|"star"|"snow"|"wave"，与天气相配\n'
        "只返回 JSON。"
    )
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": settings.zhipu_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.7,
                "max_tokens": 200,
            },
            timeout=8,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        # 提取第一个 JSON 对象
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(content[start:end + 1])
        out = dict(DEFAULT_DESIGN)
        if data.get("accent") in _DESIGN_ACCENTS:
            out["accent"] = data["accent"]
        if data.get("title_band") in ("light", "dark"):
            out["title_band"] = data["title_band"]
        if data.get("deco") in _DESIGN_DECOS:
            out["deco"] = data["deco"]
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("weather llm design failed: %s", exc.__class__.__name__)
        return None


def _daily_design(style: str, today: dt.date) -> dict:
    """当日设计参数（磁盘缓存，当天固定；LLM 失败/无 key → 默认）。"""
    cache = CACHE_DIR / f"design_{today:%Y%m%d}.json"
    try:
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("style") == style:
                return data.get("design", DEFAULT_DESIGN)
    except (OSError, json.JSONDecodeError):
        pass
    # 从缓存天气数据里取摘要（避免重复请求）
    summary = "阴 30°C，最高34° 最低28°"
    try:
        wc = CACHE_DIR / "latest_weather.json"
        if wc.exists():
            w = json.loads(wc.read_text(encoding="utf-8"))
            d0 = (w.get("daily") or [{}])[0]
            summary = (f"{w['now'].get('text','')} {w['now'].get('temp','')}°C，"
                       f"最高{d0.get('tempMax','')}° 最低{d0.get('tempMin','')}°")
    except (OSError, KeyError, json.JSONDecodeError):
        pass
    design = _llm_design_json(style, summary) or dict(DEFAULT_DESIGN)
    try:
        cache.write_text(json.dumps({"style": style, "design": design}, ensure_ascii=False),
                         encoding="utf-8")
    except OSError:
        pass
    return design


# ═══════════════════════ 数据获取 ═══════════════════════

def fetch_weather() -> dict | None:
    """拉取实时天气 + 3 日预报。无 key/失败返回 None。"""
    if not settings.qweather_key:
        return None
    base = QWEATHER_BASE.format(host=settings.qweather_host)
    try:
        loc, city = resolve_location()
        params = {"location": loc, "key": settings.qweather_key}
        now = requests.get(f"{base}/now", params=params, timeout=10).json()
        f3d = requests.get(f"{base}/3d", params=params, timeout=10).json()
        if now.get("code") != "200" or f3d.get("code") != "200":
            logger.warning("qweather error: now=%s 3d=%s", now.get("code"), f3d.get("code"))
            return None
        now_d = now["now"]
        now_d["city"] = city or settings.qweather_city
        out = {"now": now_d, "daily": f3d.get("daily", [])}
        try:
            (CACHE_DIR / "latest_weather.json").write_text(
                json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_weather failed: %s", exc.__class__.__name__)
        return None


def _fetch_city() -> str:
    """城市名：优先配置，其次 GeoAPI（失败返回空）。"""
    if settings.qweather_city:
        return settings.qweather_city
    try:
        r = requests.get(
            "https://geoapi.qweather.com/v2/city/lookup",
            params={"location": settings.qweather_location, "key": settings.qweather_key},
            timeout=10,
        )
        loc = (r.json().get("location") or [{}])[0]
        return f"{loc.get('adm1','')}·{loc.get('name','')}".lstrip("·")
    except Exception:  # noqa: BLE001
        return ""


# ═══════════════════════ 共享绘制 ═══════════════════════

from app.epd_image import SPECTRA6_PALETTE  # noqa: E402
BLACK, WHITE, YELLOW, RED, BLUE, GREEN = SPECTRA6_PALETTE
ACCENT_RGB = {"black": BLACK, "red": RED, "blue": BLUE, "green": GREEN, "yellow": YELLOW}


def _draw_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, s: int, kind: str,
               on_dark: bool = False) -> None:
    """天气图标：白色主体 + 深色描边 + 彩色点缀（on_dark=True 用于深色背景）。"""
    body, edge = (WHITE, None) if on_dark else (WHITE, BLACK)
    accent = YELLOW if on_dark else YELLOW
    drop = YELLOW if on_dark else BLUE   # 深色背景上雨滴用黄，避免蓝底/绿底看不清
    stroke = 0 if on_dark else max(4, s // 8)
    outline = None if on_dark else edge

    if kind == "sun":
        for i in range(12):
            a = i * math.pi / 6
            x1, y1 = cx + int((s + 5) * math.cos(a)), cy + int((s + 5) * math.sin(a))
            x2, y2 = cx + int((s + 13) * math.cos(a)), cy + int((s + 13) * math.sin(a))
            draw.line([x1, y1, x2, y2], fill=accent, width=max(4, s // 9))
        draw.ellipse([cx - s, cy - s, cx + s, cy + s], fill=YELLOW)
        draw.ellipse([cx - s // 3, cy - s // 3, cx + s // 6, cy + s // 6], fill=RED)
    elif kind in ("cloud", "rain", "snow", "storm"):
        # 云（两个圆拼合）
        draw.ellipse([cx - s, cy - s // 3, cx + s, cy + s // 3 + 4], fill=body, outline=outline, width=stroke)
        draw.ellipse([cx - s // 2, cy - s * 3 // 4, cx + s // 2, cy - s // 8], fill=body, outline=outline, width=stroke)
        draw.ellipse([cx - s * 7 // 10, cy - s // 8, cx + s * 3 // 10, cy + s // 3], fill=body)
        if kind == "rain":
            for dx in (-s // 2, 0, s // 2):
                x1, y1 = cx + dx, cy + s // 2
                draw.line([x1, y1, x1 - s // 7, y1 + s * 3 // 5], fill=drop, width=max(4, s // 8))
                draw.ellipse([x1 - s // 7 - s // 9, y1 + s * 3 // 5 - s // 12,
                              x1 - s // 7 + s // 9, y1 + s * 3 // 5 + s // 12], fill=drop)
        elif kind == "snow":
            for dx in (-s // 2, 0, s // 2):
                for dy in (s // 2, s * 9 // 10):
                    draw.ellipse([cx + dx - s // 8, cy + dy - s // 12,
                                  cx + dx + s // 8, cy + dy + s // 12], fill=BLACK if not on_dark else WHITE)
        elif kind == "storm":
            draw.line([cx - s // 3, cy + s // 2, cx + s // 12, cy + s * 4 // 5], fill=YELLOW, width=max(5, s // 7))
            draw.line([cx + s // 4, cy + s * 3 // 5, cx + s // 2, cy + s * 19 // 20], fill=YELLOW, width=max(5, s // 7))
    elif kind == "fog":
        for i, dy in enumerate((-s * 2 // 5, 0, s * 2 // 5)):
            w = int(s * (1.6 if i == 1 else 1.1))
            draw.line([cx - w, cy + dy, cx + w, cy + dy],
                      fill=BLACK if not on_dark else WHITE, width=max(5, s // 7))


def _draw_deco(draw: ImageDraw.ImageDraw, W: int, deco: str) -> None:
    """角落装饰符号（右下角，浅色）。"""
    x, y, s = W - 150, 1050, 26
    if deco == "sun":
        for i in range(12):
            a = i * math.pi / 6
            draw.line([x + int((s + 3) * math.cos(a)), y + int((s + 3) * math.sin(a)),
                       x + int((s + 8) * math.cos(a)), y + int((s + 8) * math.sin(a))],
                      fill=(230, 230, 230), width=4)
        draw.ellipse([x - s, y - s, x + s, y + s], fill=(230, 230, 230))
    elif deco == "star":
        pts = []
        for i in range(10):
            a = -math.pi / 2 + i * math.pi / 5
            r = s if i % 2 == 0 else s // 2
            pts.append((x + r * math.cos(a), y + r * math.sin(a)))
        draw.polygon(pts, fill=(230, 230, 230))
    elif deco == "snow":
        for i in range(6):
            a = i * math.pi / 3
            draw.line([x - int(s * 1.2 * math.cos(a)), y - int(s * 1.2 * math.sin(a)),
                       x + int(s * 1.2 * math.cos(a)), y + int(s * 1.2 * math.sin(a))],
                      fill=(230, 230, 230), width=4)
    elif deco == "wave":
        for i in range(3):
            draw.arc([x - 60 + i * 20, y - 26, x + 60 + i * 20, y + 26], 200, 340,
                     fill=(230, 230, 230), width=5)


@dataclass
class WCtx:
    """一次渲染的共享上下文。"""
    draw: ImageDraw.ImageDraw
    W: int
    H: int
    fonts: dict[str, ImageFont.FreeTypeFont]
    now: dt.datetime
    lunar: object
    weekday: str
    city: str
    temp: str
    text: str
    kind: str
    feels: str
    hum: str
    wind: str
    daily: list[dict]
    design: dict
    on_dark: bool = False
    today_mark: str = ""   # 今日节日/节气（红字标注）
    soon_marks: str = ""   # 近期节日/节气预告，如 "立秋 8/7 · 七夕 8/19"

    @property
    def accent(self) -> tuple[int, int, int]:
        return ACCENT_RGB[self.design.get("accent", "black")]

    def f(self, name: str) -> ImageFont.FreeTypeFont:
        return self.fonts[name]


def _fonts(fp: str) -> dict[str, ImageFont.FreeTypeFont]:
    return {
        "xs": ImageFont.truetype(fp, 26),
        "s": ImageFont.truetype(fp, 30),
        "m": ImageFont.truetype(fp, 40),
        "l": ImageFont.truetype(fp, 54),
        "xl": ImageFont.truetype(fp, 90),
        "xxl": ImageFont.truetype(fp, 150),
        "num": ImageFont.truetype(fp, 196),
    }


# ═══════════════════════ 各风格排版 ═══════════════════════

def _paint_apple(c: WCtx) -> None:
    """Apple 天气小组件风：白底、超大温度、信息单列、底部三日。"""
    d, W = c.draw, c.W
    black, gray = BLACK, (120, 120, 120)
    light = c.design.get("title_band") == "dark"
    date_color, sub_color = (WHITE if light else BLACK), (WHITE if light else gray)
    if light:
        d.rectangle([0, 0, W, 150], fill=BLACK)
    d.text((80, 46), f"{c.now.month}月{c.now.day}日 星期{c.weekday}", font=c.f("l"), fill=date_color)
    if c.today_mark:
        x = 80 + d.textlength(f"{c.now.month}月{c.now.day}日 星期{c.weekday}", font=c.f("l")) + 20
        d.text((x, 52), c.today_mark, font=c.f("m"), fill=YELLOW if light else RED)
    lunar_txt = f"农历{c.lunar.getMonthInChinese()}月{c.lunar.getDayInChinese()} · {c.city}"
    d.text((80, 118), lunar_txt, font=c.f("xs"), fill=sub_color)
    if c.soon_marks:
        d.text((80, 162), f"近 {c.soon_marks}", font=c.f("xs"), fill=sub_color)
    d.text((W - 80, 48), c.text, font=c.f("m"), fill=date_color, anchor="ra")
    _draw_icon(d, W - 250, 160, 44, c.kind, on_dark=light)
    # 大温度
    d.text((80, 290), c.temp, font=c.f("num"), fill=c.accent)
    d.text((520, 448), c.text, font=c.f("m"), fill=BLACK)
    d0 = c.daily[0] if c.daily else {}
    d.text((80, 560), f"最高 {d0.get('tempMax','')}°   最低 {d0.get('tempMin','')}°", font=c.f("m"), fill=BLACK)
    d.rectangle([80, 660, W - 80, 668], fill=BLACK)
    d.text((80, 720), f"体感 {c.feels}°", font=c.f("m"), fill=BLACK)
    d.text((80, 795), f"湿度 {c.hum}%", font=c.f("m"), fill=BLACK)
    d.text((80, 870), f"风力 {c.wind}", font=c.f("m"), fill=BLACK)
    # 底部三日（左右安全边距，三列均匀）
    d.rectangle([80, 970, W - 80, 976], fill=(220, 220, 220))
    for i, day in enumerate(c.daily[:3]):
        x0 = 70 + i * 487
        ddate = dt.date.fromisoformat(day["fxDate"])
        label = "今天" if i == 0 else f"周{'一二三四五六日'[ddate.weekday()]}"
        _draw_icon(d, x0 + 64, 1090, 40, WMO.get(day.get("iconDay", ""), ("", "cloud"))[1])
        d.text((x0 + 140, 1050), f"{label}  {_norm_text(day.get('textDay',''))}", font=c.f("m"), fill=BLACK)
        d.text((x0 + 140, 1110), f"{day.get('tempMin','')}°  {day.get('tempMax','')}°", font=c.f("m"), fill=BLACK)


def _paint_blue(c: WCtx) -> None:
    """多彩卡片风：蓝色信息条 + 黄色大温度 + 彩色详情点 + 黄/绿/白三色预报卡。"""
    d, W = c.draw, c.W
    # 顶部蓝色信息条：日期 + 今日节日（黄），右侧城市，底部农历
    d.rectangle([0, 0, W, 140], fill=BLUE)
    d.text((80, 34), f"{c.now.month}月{c.now.day}日 星期{c.weekday}", font=c.f("l"), fill=WHITE)
    if c.today_mark:
        x = 80 + d.textlength(f"{c.now.month}月{c.now.day}日 星期{c.weekday}", font=c.f("l")) + 20
        d.text((x, 40), c.today_mark, font=c.f("m"), fill=YELLOW)
    d.text((W - 80, 44), c.city, font=c.f("s"), fill=WHITE, anchor="ra")
    d.text((80, 100), f"农历{c.lunar.getMonthInChinese()}月{c.lunar.getDayInChinese()}",
           font=c.f("xs"), fill=WHITE)
    if c.soon_marks:
        d.text((80, 154), f"近 {c.soon_marks}", font=c.f("xs"), fill=(130, 130, 130))
    # 大温度（黄）+ 现象 + 彩色大图标
    d.text((100, 210), c.temp, font=c.f("xxl"), fill=YELLOW)
    d.text((130, 430), c.text, font=c.f("m"), fill=BLACK)
    _draw_icon(d, 950, 320, 92, c.kind)
    # 详情行：彩色圆点 + 黑字（红/蓝/绿）
    items = [(f"体感 {c.feels}°", RED), (f"湿度 {c.hum}%", BLUE), (f"风力 {c.wind}", GREEN)]
    for i, (txt, color) in enumerate(items):
        x = 100 + i * 380
        d.ellipse([x, 578, x + 26, 604], fill=color)
        d.text((x + 44, 566), txt, font=c.f("m"), fill=BLACK)
    d.line([(80, 660), (W - 80, 660)], fill=(200, 200, 200), width=3)
    # 底部三日：黄 / 绿 / 白(蓝描边) 三色卡片，左右留安全边距
    card = [(YELLOW, BLACK), (GREEN, BLACK), (WHITE, BLACK)]
    for i, day in enumerate(c.daily[:3]):
        x0 = 80 + i * 490
        bg, fg = card[i]
        d.rectangle([x0, 740, x0 + 450, 1090], fill=bg, outline=BLUE, width=5)
        ddate = dt.date.fromisoformat(day["fxDate"])
        label = "今天" if i == 0 else f"周{'一二三四五六日'[ddate.weekday()]}"
        _draw_icon(d, x0 + 78, 870, 50, WMO.get(day.get("iconDay", ""), ("", "cloud"))[1],
                   on_dark=(bg == GREEN))
        d.text((x0 + 165, 775), f"{label}  {_norm_text(day.get('textDay',''))}", font=c.f("m"), fill=fg)
        d.text((x0 + 165, 855), f"{day.get('tempMin','')}°  {day.get('tempMax','')}°", font=c.f("s"), fill=fg)


def _paint_magazine(c: WCtx) -> None:
    """杂志排版风：顶部深色信息条 + 左温度右大图标 + 底部信息条。"""
    d, W = c.draw, c.W
    dark_band = c.design.get("title_band") != "light"
    band = BLACK if dark_band else (230, 230, 230)
    txt = WHITE if dark_band else BLACK
    d.rectangle([0, 0, W, 150], fill=band)
    d.text((80, 46), f"{c.now.month}月{c.now.day}日 星期{c.weekday}", font=c.f("l"), fill=txt)
    if c.today_mark:
        x = 80 + d.textlength(f"{c.now.month}月{c.now.day}日 星期{c.weekday}", font=c.f("l")) + 20
        d.text((x, 52), c.today_mark, font=c.f("m"), fill=YELLOW if dark_band else RED)
    d.text((W - 80, 56), f"农历{c.lunar.getMonthInChinese()}月{c.lunar.getDayInChinese()}",
           font=c.f("s"), fill=txt, anchor="ra")
    if c.soon_marks:
        d.text((80, 104), f"近 {c.soon_marks}", font=c.f("xs"), fill=YELLOW if dark_band else (150, 90, 0))
    d.text((110, 290), c.temp, font=c.f("num"), fill=c.accent)
    d.text((140, 540), c.text, font=c.f("m"), fill=BLACK)
    _draw_icon(d, 1180, 430, 100, c.kind)
    d.line([(90, 640), (W - 90, 640)], fill=BLACK, width=5)
    d.text((110, 700), f"体感 {c.feels}°", font=c.f("m"), fill=BLACK)
    d.text((560, 700), f"湿度 {c.hum}%", font=c.f("m"), fill=BLACK)
    d.text((1010, 700), f"风力 {c.wind}", font=c.f("m"), fill=BLACK)
    d0 = c.daily[0] if c.daily else {}
    d.text((110, 770), f"今日 {_norm_text(d0.get('textDay',''))}  最高 {d0.get('tempMax','')}° · 最低 {d0.get('tempMin','')}°",
           font=c.f("xs"), fill=(120, 120, 120))
    for i, day in enumerate(c.daily[:3]):
        x0 = 70 + i * 487
        ddate = dt.date.fromisoformat(day["fxDate"])
        label = "今天" if i == 0 else f"周{'一二三四五六日'[ddate.weekday()]}"
        _draw_icon(d, x0 + 70, 1010, 36, WMO.get(day.get("iconDay", ""), ("", "cloud"))[1])
        d.text((x0 + 130, 975), f"{label}  {_norm_text(day.get('textDay',''))}", font=c.f("m"), fill=BLACK)
        d.text((x0 + 130, 1045), f"{day.get('tempMin','')}°  {day.get('tempMax','')}°", font=c.f("m"), fill=BLACK)


def _paint_classic(c: WCtx) -> None:
    """经典微调：原布局 + 新图标 + 统一层级（旧版改进）。"""
    d, W = c.draw, c.W
    black, gray = BLACK, (120, 120, 120)
    d.text((60, 42), f"{c.now.month}月{c.now.day}日 星期{c.weekday}", font=c.f("l"), fill=black)
    if c.today_mark:
        x = 60 + d.textlength(f"{c.now.month}月{c.now.day}日 星期{c.weekday}", font=c.f("l")) + 20
        d.text((x, 48), c.today_mark, font=c.f("m"), fill=RED)
    if c.soon_marks:
        d.text((60, 158), f"近 {c.soon_marks}", font=c.f("xs"), fill=(150, 90, 0))
    lunar_txt = f"农历{c.lunar.getMonthInChinese()}月{c.lunar.getDayInChinese()}"
    d.text((60, 116), lunar_txt, font=c.f("s"), fill=gray)
    city_str = f"{c.city} · 和风天气"
    cw = d.textlength(city_str, font=c.f("xs"))
    d.text((W - 60 - cw, 66), city_str, font=c.f("xs"), fill=gray)
    d.text((110, 240), c.temp, font=c.f("num"), fill=c.accent)
    d.text((140, 470), c.text, font=c.f("m"), fill=black)
    _draw_icon(d, 250, 650, 72, c.kind)
    d0 = c.daily[0] if c.daily else {}
    xr = 620
    d.text((xr, 260), f"今日  {_norm_text(d0.get('textDay',''))}  {d0.get('tempMin','')}°~{d0.get('tempMax','')}°",
           font=c.f("s"), fill=black)
    d.text((xr, 330), f"体感 {c.feels}°    湿度 {c.hum}%", font=c.f("s"), fill=gray)
    d.text((xr, 400), f"风 {c.wind}", font=c.f("s"), fill=gray)
    d.line([(xr, 470), (W - 60, 470)], fill=(220, 220, 220), width=3)
    d.line([(60, 700), (W - 60, 700)], fill=(220, 220, 220), width=3)
    for i, day in enumerate(c.daily[1:3]):
        x0 = 100 + i * 760
        ddate = dt.date.fromisoformat(day["fxDate"])
        dd, dw = ddate.day, "一二三四五六日"[ddate.weekday()]
        _draw_icon(d, x0 + 50, 800, 36, WMO.get(day.get("iconDay", ""), ("", "cloud"))[1])
        d.text((x0 + 120, 762), f"{dd}日 周{dw}", font=c.f("s"), fill=black)
        d.text((x0 + 120, 810), f"{_norm_text(day.get('textDay',''))}  {day.get('tempMin','')}°~{day.get('tempMax','')}°",
               font=c.f("s"), fill=gray)


_PAINTERS = {"apple": _paint_apple, "blue": _paint_blue, "magazine": _paint_magazine, "classic": _paint_classic}


def _render_card(style: str, data: dict, design: dict) -> Image.Image:
    """按风格渲染 1600x1200（横屏用户视角）RGB 图。"""
    from app.epd_image import DEVICE_HEIGHT, DEVICE_WIDTH
    W, H = DEVICE_HEIGHT, DEVICE_WIDTH
    img = Image.new("RGB", (W, H), WHITE)
    draw = ImageDraw.Draw(img)
    fonts = _fonts(find_cjk_font())
    now = dt.datetime.now()
    lunar = Solar.fromDate(now).getLunar()
    weekday = "一二三四五六日"[now.weekday()]
    now_d = data["now"]
    d0 = data.get("daily", [{}])[0]
    c = WCtx(
        draw=draw, W=W, H=H, fonts=fonts, now=now, lunar=lunar, weekday=weekday,
        city=now_d.get("city") or "",
        temp=f"{int(float(now_d.get('temp', 0)))}°",
        text=_norm_text(now_d.get("text") or ""),
        kind=WMO.get(now_d.get("icon", ""), ("", "cloud"))[1],
        feels=_norm_text(now_d.get("feelsLike", "")),
        hum=now_d.get("humidity", ""),
        wind=f"{_norm_text(now_d.get('windDir',''))}{now_d.get('windScale','')}级",
        daily=data.get("daily", []),
        design=design,
        today_mark=_day_mark(now.date()),
        soon_marks=_calendar_info(now.date())[1],
    )
    _PAINTERS[style](c)
    # 蓝底风格装饰符号不可见，跳过
    if style != "blue":
        _draw_deco(draw, W, design.get("deco", "none"))
    return img


# ═══════════════════════ 对外入口 ═══════════════════════

# 滚动清理保留对数：帧 + 同 stem 原图按对保留最新 CACHE_KEEP 对，更旧的一并删除。
# 缓存键含日期/风格/location，超过 1 小时即不再命中，保留少量余量仅供重启复用。
CACHE_KEEP = 8


def _prune_weather_cache() -> None:
    """帧缓存滚动清理：按 mtime 保留最新 CACHE_KEEP 对，更旧的帧与
    同 stem 原图一并删除（原图与帧同生同灭，不残留孤儿文件）。"""
    mtimes: dict[str, float] = {}
    for f in CACHE_DIR.glob("weather_*"):
        if f.suffix not in (".fps6", ".png"):
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        mtimes[f.stem] = max(mtimes.get(f.stem, 0.0), mtime)
    stale = sorted(mtimes.items(), key=lambda kv: kv[1], reverse=True)[CACHE_KEEP:]
    for stem, _ in stale:
        for suffix in (".fps6", ".png"):
            try:
                (CACHE_DIR / f"{stem}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass


def _cache_stem(today: dt.date, style: str, loc_id: str) -> str:
    """当日缓存文件名 stem：日期 + 风格 + location 指纹（帧与原图共用）。"""
    return f"weather_{today:%Y%m%d}_{style}_{hashlib.md5(loc_id.encode()).hexdigest()[:6]}"


def _current_stem() -> str:
    """以当前时刻（今日风格 + 当前 location）算出的缓存 stem。"""
    today = dt.date.today()
    return _cache_stem(today, pick_style(today), resolve_location()[0])


def render_weather_card() -> bytes | None:
    """渲染当日风格天气卡片 → FPS6 字节（当日缓存 + 1 小时数据刷新）。无数据返回 None。

    卡片按“相框横放”的视角排版（1600x1200），prepare_image 会把横图旋转
    90° 转成 FPS6 竖屏数据——横放相框观看时卡片正立。
    每日 0 点后自动切换风格（缓存文件名含日期+风格）。
    缓存指纹带 location：管理台改了城市后，缓存自动失效立即按新城市渲染。
    渲染成功时同 stem 落盘原始彩色卡片 PNG（横屏视角）——预览由原图缩放
    而来（ADR-0001），原图与帧同批滚动清理（见 _prune_weather_cache）。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stem = _current_stem()
    cache = CACHE_DIR / f"{stem}.fps6"
    if cache.exists() and (dt.datetime.now().timestamp() - cache.stat().st_mtime) < 3600:
        return cache.read_bytes()

    data = fetch_weather()
    if not data:
        return None
    style = pick_style()
    design = _daily_design(style, dt.date.today())
    img = _render_card(style, data, design)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    prepared = prepare_image(png, dither=True)  # 横图自动旋转为 FPS6 竖屏数据
    # 先落盘原图（尽力而为），再写帧缓存：帧写失败才视为渲染失败
    try:
        (CACHE_DIR / f"{stem}.png").write_bytes(png)
    except OSError:
        logger.warning("weather original save failed: %s", stem)
    cache.write_bytes(prepared.data)
    _prune_weather_cache()
    return prepared.data


def weather_original_png() -> bytes | None:
    """当日天气卡片原图（横屏视角彩色 PNG）字节；未渲染/文件缺失返回 None。

    与帧缓存同 stem：render_weather_card() 渲染成功后必存在。
    预览永远由原图缩放而来（ADR-0001），不从帧还原。
    """
    png = CACHE_DIR / f"{_current_stem()}.png"
    try:
        return png.read_bytes() or None
    except OSError:
        return None


def render_weather_slot() -> dict | None:
    """对外入口：渲染天气卡片，返回内容清单字段；不可用返回 None。

    清单字段（含 memory_score/landscape）在此一次成型，不含 url/preview_url
    ——源不负责 url（ADR-0002），清单 url 由路由层 helper 统一组装。
    """
    data = render_weather_card()
    if not data:
        return None
    content_id = "weather-" + hashlib.sha256(data).hexdigest()[:10]
    now = dt.datetime.now()
    return {
        "id": content_id,
        "filename": "weather_card",
        "caption": f"天气 · {STYLE_LABELS.get(pick_style(), pick_style())}",
        "date": now.strftime("%Y.%m.%d"),
        "memory_score": None,
        "width": 1200,
        "height": 1600,
        "landscape": 1,   # 卡片横屏
    }


# ═══════════════════════ 内容源适配器（ADR-0002） ═══════════════════════

class WeatherSource:
    """天气卡片内容源：weather- 前缀的渲染命名空间（当前内容 = 当日卡片）。

    id 是当日渲染帧的内容指纹（设备变更检测用），不是查找键。
    清单字段不含 url/preview_url（源不负责 url，见 ADR-0002）。
    """

    id_prefix = "weather-"
    missing_detail = "no weather card"

    def meta(self) -> dict | None:
        """确保当日卡片已渲染，返回内容清单字段；不可用返回 None。"""
        return render_weather_slot()

    def render(self, content_id: str) -> bytes | None:
        """当日天气卡片的 FPS6 帧（当日缓存 + 1 小时数据刷新）；不可用返回 None。"""
        return render_weather_card()

    def original(self, content_id: str) -> bytes | None:
        """当日卡片原图（横屏视角彩色 PNG）字节；未渲染/缺失返回 None。"""
        return weather_original_png()


SOURCE = WeatherSource()
