"""weather_card.py — 天气 + 日历卡片（0 点后到 10 点时段显示）。

数据源：和风天气（devapi.qweather.com，QWEATHER_KEY）。
渲染：PIL 排版（中文用 find_cjk_font）→ FPS6（epd_image.prepare_image）。
无 key / 请求失败时：render_weather_slot() 返回 None，调用方回退其他时段内容。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
from lunar_python import Solar

from app.config import settings
from app.epd_image import find_cjk_font, prepare_image

logger = logging.getLogger(__name__)

QWEATHER_BASE = "https://devapi.qweather.com/v7/weather"
CACHE_DIR = Path(__file__).resolve().parent / "weather_cache"
CACHE_DIR.mkdir(exist_ok=True)

# 和风天气现象代码 → (中文, 简单图标风格)
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


def fetch_weather() -> dict | None:
    """拉取实时天气 + 3 日预报。无 key/失败返回 None。"""
    if not settings.qweather_key:
        return None
    try:
        params = {"location": settings.qweather_location, "key": settings.qweather_key}
        now = requests.get(f"{QWEATHER_BASE}/now", params=params, timeout=10).json()
        f3d = requests.get(f"{QWEATHER_BASE}/3d", params=params, timeout=10).json()
        if now.get("code") != "200" or f3d.get("code") != "200":
            logger.warning("qweather error: now=%s 3d=%s", now.get("code"), f3d.get("code"))
            return None
        now_d = now["now"]
        now_d["city"] = now.get("fxLink", "") and _fetch_city() or settings.qweather_location
        return {"now": now_d, "daily": f3d.get("daily", [])}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_weather failed: %s", exc.__class__.__name__)
        return None


def _fetch_city() -> str:
    """城市名（GeoAPI 反向，失败返回 location）。"""
    try:
        r = requests.get(
            "https://geoapi.qweather.com/v2/city/lookup",
            params={"location": settings.qweather_location, "key": settings.qweather_key},
            timeout=10,
        ).json()
        loc = (r.get("location") or [{}])[0]
        return f"{loc.get('adm1','')}·{loc.get('name','')}"
    except Exception:  # noqa: BLE001
        return ""


def _draw_weather_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int,
                       kind: str, fill: tuple[int, int, int]) -> None:
    """简单天气图形：sun / cloud / rain / storm / snow / fog。"""
    from app.epd_image import SPECTRA6_PALETTE
    yellow = SPECTRA6_PALETTE[4]   # 黄
    orange = SPECTRA6_PALETTE[5]   # 橙
    blue = SPECTRA6_PALETTE[1]     # 蓝
    gray = SPECTRA6_PALETTE[2]     # 灰
    dark = SPECTRA6_PALETTE[3]     # 黑

    if kind == "sun":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=yellow)
        for i in range(8):
            import math
            a = i * math.pi / 4
            x1, y1 = cx + int((r + 4) * math.cos(a)), cy + int((r + 4) * math.sin(a))
            x2, y2 = cx + int((r + 14) * math.cos(a)), cy + int((r + 14) * math.sin(a))
            draw.line([x1, y1, x2, y2], fill=orange, width=5)
    elif kind == "cloud":
        draw.ellipse([cx - r, cy - r // 3, cx + r, cy + r // 3 + 6], fill=gray)
        draw.ellipse([cx - r // 2, cy - r, cx + r // 2, cy], fill=gray)
    elif kind == "rain":
        draw.ellipse([cx - r, cy - r // 3, cx + r, cy + r // 3 + 6], fill=gray)
        draw.ellipse([cx - r // 2, cy - r, cx + r // 2, cy], fill=gray)
        for dx in (-r // 2, 0, r // 2):
            draw.line([cx + dx, cy + r // 2, cx + dx - 8, cy + r // 2 + 16], fill=blue, width=5)
    elif kind == "storm":
        draw.ellipse([cx - r, cy - r // 3, cx + r, cy + r // 3 + 6], fill=gray)
        draw.line([cx - r // 3, cy + r // 2, cx - r // 3 + 10, cy + r // 2 + 20], fill=yellow, width=6)
        draw.line([cx + r // 3, cy + r // 2 + 8, cx + r // 3 + 10, cy + r // 2 + 28], fill=yellow, width=6)
    elif kind == "snow":
        draw.ellipse([cx - r, cy - r // 3, cx + r, cy + r // 3 + 6], fill=gray)
        for dx in (-r // 2, 0, r // 2):
            for dy in (8, 18):
                draw.ellipse([cx + dx - 4, cy + dy, cx + dx + 4, cy + dy + 8], fill=dark)
    elif kind == "fog":
        for dy in (-6, 4, 14):
            draw.line([cx - r, cy + dy, cx + r, cy + dy], fill=gray, width=6)


def render_weather_card() -> bytes | None:
    """渲染天气卡片 → FPS6 字节（缓存 1 小时）。无数据返回 None。"""
    cache = CACHE_DIR / "weather.fps6"
    if cache.exists() and (dt.datetime.now().timestamp() - cache.stat().st_mtime) < 3600:
        return cache.read_bytes()

    data = fetch_weather()
    if not data:
        return None

    from app.epd_image import DEVICE_WIDTH, DEVICE_HEIGHT, SPECTRA6_PALETTE
    W, H = DEVICE_WIDTH, DEVICE_HEIGHT
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font_l = ImageFont.truetype(find_cjk_font(), 72)
    font_m = ImageFont.truetype(find_cjk_font(), 44)
    font_s = ImageFont.truetype(find_cjk_font(), 34)
    font_xs = ImageFont.truetype(find_cjk_font(), 26)
    black, gray = (0, 0, 0), (120, 120, 120)
    blue = SPECTRA6_PALETTE[1]

    now = dt.datetime.now()
    lunar = Solar.fromDate(now).getLunar()
    weekday = "一二三四五六日"[now.weekday()]

    now_d = data["now"]
    text = _norm_text(now_d.get("text") or "")
    kind = WMO.get(now_d.get("icon", ""), ("", "cloud"))[1]
    daily = data.get("daily", [])

    # 顶部：日期 + 星期 + 农历
    draw.text((60, 40), f"{now.month}月{now.day}日 星期{weekday}", font=font_l, fill=black)
    draw.text((60, 140), f"农历{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}",
              font=font_m, fill=gray)

    # 中部：图标 + 当前温度 + 现象
    _draw_weather_icon(draw, W // 2 - 220, 360, 70, kind, black)
    temp = f"{int(float(now_d.get('temp', 0)))}°"
    draw.text((W // 2 - 130, 280), temp, font=font_l, fill=blue)
    draw.text((W // 2 - 130, 380), text, font=font_m, fill=black)

    # 中下：高低温/体感/湿度/风
    d0 = daily[0] if daily else {}
    draw.text((60, 520), f"今日 {_norm_text(d0.get('textDay',''))}  {d0.get('tempMin','')}°~{d0.get('tempMax','')}°",
              font=font_s, fill=black)
    line = (f"体感 {_norm_text(now_d.get('feelsLike',''))}°    "
            f"湿度 {now_d.get('humidity','')}%    "
            f"风 {_norm_text(now_d.get('windDir',''))}{now_d.get('windScale','')}级")
    draw.text((60, 590), line, font=font_s, fill=gray)

    # 底部：未来 2 天
    for i, d in enumerate(daily[1:3]):
        y = 720 + i * 130
        ddate = dt.date.fromisoformat(d["fxDate"])
        dd = ddate.day
        dw = "一二三四五六日"[ddate.weekday()]
        icon_kind = WMO.get(d.get("iconDay", ""), ("", "cloud"))[1]
        _draw_weather_icon(draw, 130, y + 40, 32, icon_kind, black)
        draw.text((200, y), f"{dd}日 周{dw}  {_norm_text(d.get('textDay',''))}  "
                            f"{d.get('tempMin','')}°~{d.get('tempMax','')}°",
                  font=font_s, fill=black)

    # 底部城市
    city = now_d.get("city") or ""
    draw.text((60, H - 80), f"{city} · 和风天气", font=font_xs, fill=gray)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    prepared = prepare_image(buf.getvalue(), W, H, dither=True)
    cache.write_bytes(prepared.data)
    return prepared.data


def render_weather_slot() -> dict | None:
    """对外入口：渲染天气卡片。返回 content 元数据或 None（无数据/不可用）。"""
    data = render_weather_card()
    if not data:
        return None
    import hashlib
    content_id = "weather-" + hashlib.sha256(data).hexdigest()[:10]
    now = dt.datetime.now()
    return {
        "id": content_id,
        "filename": "weather_card",
        "caption": "",
        "date": now.strftime("%Y.%m.%d"),
        "width": 1200,
        "height": 1600,
        "url": "/api/images/weather/raw",
        "preview_url": "/api/images/weather/preview",
    }
