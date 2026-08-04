"""news_card.py — 热点新闻卡片（21 点后时段显示）。

新闻源：60s API（https://60s.viki.moe/v2/60s，免费无 key，当天热点新闻摘要）。
配图：imagegen_url/key/model（OpenAI 图片兼容接口，如即梦/通义/DashScope 兼容模式）。
降级链：文生图失败 → 程序文字卡片（新闻标题排版）；新闻也拿不到 → None（回退照片时段）。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import logging
import textwrap
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.epd_image import DEVICE_HEIGHT, DEVICE_WIDTH, SPECTRA6_PALETTE, find_cjk_font, prepare_image

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / "news_cache"
CACHE_DIR.mkdir(exist_ok=True)

NEWS_COUNT = 5  # 卡片展示条数
NEWS_API = "https://60s.viki.moe/v2/60s"


def _fetch_60s() -> list[str] | None:
    """60s API：返回当天热点新闻列表。失败返回 None。"""
    try:
        r = requests.get(NEWS_API, timeout=15)
        r.raise_for_status()
        data = r.json()
        news = (data.get("data") or {}).get("news") or []
        titles = [str(x).strip() for x in news if str(x).strip()]
        return titles[:NEWS_COUNT] or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_top_news failed: %s", exc.__class__.__name__)
        return None


# 智谱 MCP 搜索的分领域 query（泛化新闻词会被内容过滤拦截，分领域更稳）
ZHIPU_QUERIES = ["今日体育新闻", "今日财经新闻", "今日科技新闻", "今日娱乐新闻", "今日健康新闻"]


def _fetch_zhipu() -> list[str] | None:
    """智谱 MCP 分领域搜索合并去重。被过滤/失败返回 None。"""
    from app.zhipu_mcp import search_news
    key = settings.zhipu_api_key
    if not key:
        return None
    titles, seen = [], set()
    for q in ZHIPU_QUERIES:
        items = search_news(key, q, count=4)
        if not items:
            continue
        for it in items:
            t = it.get("title", "").strip()
            if t and len(t) > 2 and t not in seen:
                seen.add(t)
                titles.append(t)
        if len(titles) >= NEWS_COUNT:
            break
    return titles[:NEWS_COUNT] or None


def fetch_top_news() -> list[str] | None:
    """热点新闻：NEWS_SOURCE=zhipu 时优先智谱 MCP，否则/失败降级 60s API。"""
    if settings.news_source == "zhipu":
        t = _fetch_zhipu()
        if t:
            return t
        logger.info("zhipu news empty, fallback 60s")
    return _fetch_60s()


def _generate_cartoon(prompt: str) -> bytes | None:
    """文生图（OpenAI 图片接口兼容）。返回 PNG 字节或 None。"""
    if not (settings.imagegen_url and settings.imagegen_key and settings.imagegen_model):
        return None
    try:
        r = requests.post(
            settings.imagegen_url,
            headers={"Authorization": f"Bearer {settings.imagegen_key}"},
            json={"model": settings.imagegen_model, "prompt": prompt,
                  "n": 1, "size": "1024x1536", "response_format": "b64_json"},
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        b64 = data["data"][0].get("b64_json")
        if not b64:
            return None
        return base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001
        logger.warning("imagegen failed: %s", exc.__class__.__name__)
        return None


def _render_text_card(titles: list[str]) -> bytes:
    """降级：新闻标题文字卡片（暖色卡通背景 + 圆角卡片）。"""
    W, H = DEVICE_WIDTH, DEVICE_HEIGHT
    img = Image.new("RGB", (W, H), (255, 247, 230))
    draw = ImageDraw.Draw(img)
    font_title = ImageFont.truetype(find_cjk_font(), 52)
    font_item = ImageFont.truetype(find_cjk_font(), 38)
    black, orange, gray = (60, 40, 20), (240, 120, 40), (130, 110, 90)

    draw.text((56, 48), "🔥 今日热点", font=font_title, fill=orange)

    y = 150
    for i, title in enumerate(titles[:NEWS_COUNT], 1):
        # 圆角卡片
        draw.rounded_rectangle([40, y - 18, W - 40, y + 62], radius=18, fill=(255, 255, 255),
                               outline=(255, 220, 180), width=2)
        draw.text((56, y), f"{i}.", font=font_item, fill=orange)
        lines = textwrap.wrap(title, width=22)
        ty = y
        for ln in lines[:2]:
            draw.text((120, ty), ln, font=font_item, fill=black)
            ty += 46
        y += 140

    draw.text((56, H - 70), "新闻来源：智谱搜索", font=ImageFont.truetype(find_cjk_font(), 24), fill=gray)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _build_prompt(titles: list[str]) -> str:
    top = titles[0] if titles else "今日热点"
    rest = "；".join(titles[1:4])
    return (f"温馨可爱的卡通插画风格（扁平、暖色调、圆润），主题概括这条新闻：{top}。"
            f"其他相关热点：{rest}。无文字，无水印，竖构图。")


_cache_fps6: bytes | None = None
_cache_ts: float = 0


def render_news_fps6() -> bytes | None:
    """渲染新闻卡片 FPS6（缓存 1 小时）。新闻不可用返回 None。"""
    global _cache_fps6, _cache_ts
    if _cache_fps6 and (dt.datetime.now().timestamp() - _cache_ts) < 3600:
        return _cache_fps6
    titles = fetch_top_news()
    if not titles:
        return None
    data = None
    if settings.imagegen_url:
        raw = _generate_cartoon(_build_prompt(titles))
        if raw:
            try:
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                data = prepare_image(buf.getvalue(), DEVICE_WIDTH, DEVICE_HEIGHT, dither=True).data
            except Exception as exc:  # noqa: BLE001
                logger.warning("imagegen decode failed: %s", exc)
                data = None
    if not data:
        data = prepare_image(_render_text_card(titles), DEVICE_WIDTH, DEVICE_HEIGHT,
                             dither=True).data
    _cache_fps6, _cache_ts = data, dt.datetime.now().timestamp()
    return data


def render_news_slot() -> dict | None:
    """对外入口：渲染新闻卡片 → 返回 content 元数据。新闻不可用返回 None。"""
    data = render_news_fps6()
    if not data:
        return None
    titles = fetch_top_news() or []
    content_id = "news-" + hashlib.sha256(data).hexdigest()[:10]
    return {
        "id": content_id,
        "filename": "news_card",
        "caption": titles[0] if titles else "",
        "date": dt.datetime.now().strftime("%Y.%m.%d"),
        "width": DEVICE_WIDTH,
        "height": DEVICE_HEIGHT,
        "url": "/api/images/news/raw",
        "preview_url": "/api/images/news/preview",
    }
