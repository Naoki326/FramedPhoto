"""calendar_card.py — 日历卡片（一页月历 + 每日家庭寄语）。

渲染：PIL 排版一页当月月历（公历 + 农历 + 节气/节日）→ FPS6
（epd_image.prepare_image）。纯本地计算（lunar_python），无网络依赖；
仅每日寄语可选调 LLM（无 key / 失败降级内置语料，按日期轮换稳定）。

布局（1600x1200 横屏用户视角，prepare_image 旋转成 FPS6 竖屏数据）：
    ┌──────────────────────────────────────────────┐
    │ 蓝底顶栏：2025年8月（白大字）  立秋8/7·处暑8/23 │
    │ 表头：日(红) 一 二 三 四 五 六(蓝)              │
    │ 6x7 网格：日号 + 小字（节日红/节气绿/农历黑）    │
    │   周日红、周六蓝、平日黑；今日黄底格 + 红圈      │
    │ 底部寄语条：蓝竖条 + 黑句子 + 灰落款             │
    └──────────────────────────────────────────────┘
六色全用：黑（文字/网格线）白（底）红（周日/节日/今日圈）
蓝（周六/顶栏/寄语条）绿（节气）黄（今日格底）。

缓存：当日一份（calendar_YYYYMMDD.fps6 + .png 原图，同 stem 同生同灭，
承 ADR-0001 预览由原图缩放而来），跨天文件名变化自然失效换月。
"""
from __future__ import annotations

import calendar as pycal
import datetime as dt
import hashlib
import io
import json
import logging
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
from lunar_python import Solar

from app import calendar_facts
from app.calendar_facts import jieqi, lunar_day_label, solar_festival, lunar_festival
from app.config import settings
from app.epd_image import DEVICE_HEIGHT, DEVICE_WIDTH, SPECTRA6_PALETTE, find_cjk_font, prepare_image

logger = logging.getLogger(__name__)

BLACK, WHITE, YELLOW, RED, BLUE, GREEN = SPECTRA6_PALETTE

# 今日格淡黄底：Spectra 量化抖动后呈浅黄（纯 YELLOW 过浓，盖过红圈）
TODAY_BG = (255, 240, 160)
GRID_GRAY = (215, 215, 215)     # 网格线（量化后近白，只起结构暗示）
FOOT_GRAY = (140, 140, 140)     # 底部落款

CACHE_DIR = Path(settings.calendar_cache_dir)

# ═══════════════════════ 月历数据 ═══════════════════════

# 周日开头的 Calendar 实例（实例级配置，不动 pycal 全局 firstweekday）
_CAL = pycal.Calendar(firstweekday=6)
WEEKDAYS = "日一二三四五六"


def month_grid(year: int, month: int) -> list[list[int]]:
    """当月 6x7 日期矩阵（0 = 非本月；不足 6 周补空行，网格行高稳定）。"""
    weeks = _CAL.monthdayscalendar(year, month)
    while len(weeks) < 6:
        weeks.append([0] * 7)
    return weeks[:6]


def month_jieqi(year: int, month: int, max_n: int = 2) -> list[tuple[str, int]]:
    """当月节气 [(名, 日)] 按日期升序（顶栏预告用）。"""
    out: list[tuple[str, int]] = []
    for d in range(1, 32):
        try:
            day = dt.date(year, month, d)
        except ValueError:
            break
        jq = jieqi(day)
        if jq:
            out.append((jq, d))
    return out[:max_n]


# ═══════════════════════ 每日寄语 ═══════════════════════

# 内置语料：无 LLM key / 生成失败时的稳定降级（按日期轮换，同一天固定）
FALLBACK_QUOTES = (
    "家是讲爱的地方，慢慢说，轻轻听",
    "今天也要记得对家人说一声谢谢",
    "一家人围坐吃饭，就是最好的时光",
    "你的笑容是家里最亮的光",
    "别急，好日子是一天天过出来的",
    "把今天过好，就是对未来最好的准备",
    "累了就回家，灯一直为你亮着",
    "孩子慢慢长，我们慢慢陪",
    "爱要大声说出口，趁今天",
    "平凡的一天，也值得认真度过",
    "家里的唠叨，都是没说出口的牵挂",
    "好好吃饭，好好睡觉，好好相爱",
    "每个认真生活的人，都了不起",
    "今天的主角是你，开心一点",
    "风雨再大，家永远是港湾",
    "陪伴是最长情的告白",
    "给自己一个大大的拥抱，你辛苦了",
    "日子平淡，但有你们就闪亮",
    "慢慢来，比较快",
    "此刻的团聚，就是小确幸",
    "心存善意，处处都是晴天",
    "不管多晚，总有人等你回家吃饭",
    "生活的甜，藏在小事里",
    "你已经很棒了，真的",
    "愿今天的你，被温柔以待",
    "家的温度，来自每个人的付出",
    "少一点计较，多一点拥抱",
    "今晚早点睡，明天会更好",
    "记得常回家看看",
    "幸福就是，你们都在",
    "新的一个月，全家平安顺遂",
    "把烦恼留给昨天，把微笑带回家",
)


def _quote_llm(today: dt.date, context: str) -> str | None:
    """调智谱 GLM 生成今日家庭寄语（≤18 字）。无 key / 失败返回 None。"""
    key = settings.zhipu_api_key
    if not key:
        return None
    recent = _quote_history()[-7:]
    avoid = (f"最近已用过的句子（必须避让，换一句意思不同的）：{'、'.join(recent)}。\n"
             if recent else "")
    system = (
        "你是墨水屏家庭日历的每日寄语生成器。为全家生成一句鼓励或好听的话，"
        "温暖、真诚、不鸡汤不说教，适合出现在家里的月历上。只输出 JSON，"
        '不要任何其他文字。JSON 形如：{"quote": "……"}'
    )
    user = (
        f"今天是 {today.year}年{today.month}月{today.day}日 星期{WEEKDAYS[today.isoweekday() % 7]}。"
        f"{context}\n{avoid}"
        "要求：12-18 个汉字，一句话，不含标点堆砌，不出现「加油哦~」式网络腔。"
        "只返回 JSON。"
    )
    try:
        r = requests.post(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": settings.zhipu_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.9,
                "max_tokens": 100,
            },
            timeout=8,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            return None
        quote = str(json.loads(content[start:end + 1]).get("quote", "")).strip()
        if not quote or len(quote) > 24:
            return None
        return quote
    except Exception as exc:  # noqa: BLE001
        logger.warning("calendar quote llm failed: %s", exc.__class__.__name__)
        return None


def _quote_file(today: dt.date) -> Path:
    return CACHE_DIR / f"quote_{today:%Y%m%d}.json"


def _quote_history_file() -> Path:
    return CACHE_DIR / "quote_history.json"


def _quote_history(n: int = 12) -> list[str]:
    try:
        return json.loads(_quote_history_file().read_text(encoding="utf-8"))[-n:]
    except (OSError, json.JSONDecodeError):
        return []


def _record_quote(quote: str) -> None:
    try:
        h = _quote_history(64)
        h.append(quote)
        _quote_history_file().write_text(
            json.dumps(h[-32:], ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _fallback_quote(today: dt.date) -> str:
    return FALLBACK_QUOTES[today.toordinal() % len(FALLBACK_QUOTES)]


def daily_quote(today: dt.date | None = None, context: str = "") -> str:
    """今日寄语：磁盘缓存（当天固定）> LLM 生成 > 内置语料轮换。"""
    today = today or dt.date.today()
    cache = _quote_file(today)
    try:
        if cache.exists():
            q = str(json.loads(cache.read_text(encoding="utf-8")).get("quote", "")).strip()
            if q:
                return q
    except (OSError, json.JSONDecodeError):
        pass
    quote = _quote_llm(today, context) or _fallback_quote(today)
    try:
        cache.write_text(json.dumps({"quote": quote}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    if quote not in _quote_history():
        _record_quote(quote)
    return quote


# ═══════════════════════ 渲染 ═══════════════════════

def _fonts(fp: str) -> dict[str, ImageFont.FreeTypeFont]:
    return {
        "s": ImageFont.truetype(fp, 26),
        "m": ImageFont.truetype(fp, 34),
        "l": ImageFont.truetype(fp, 44),
        "xl": ImageFont.truetype(fp, 64),
        "num": ImageFont.truetype(fp, 68),
    }


def _fit(draw: ImageDraw.ImageDraw, fp: str, text: str, font: ImageFont.FreeTypeFont,
         max_w: int) -> ImageFont.ImageFont:
    """字号自适应缩小到 max_w 内（最小降 24）。"""
    size = font.size
    while size > 24 and draw.textlength(text, font=font) > max_w:
        size -= 4
        font = ImageFont.truetype(fp, size)
    return font


def render_month(today: dt.date | None = None) -> Image.Image:
    """渲染一页当月月历 1600x1200（横屏用户视角）RGB 图。"""
    today = today or dt.date.today()
    W, H = DEVICE_HEIGHT, DEVICE_WIDTH          # 1600 x 1200
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    fp = find_cjk_font()
    f = _fonts(fp)

    # ---- 顶栏（蓝底白字：年月大字 + 当月节气预告）----
    TOP_H = 120
    d.rectangle([0, 0, W, TOP_H], fill=BLUE)
    d.text((60, 26), f"{today.year}年{today.month}月", font=f["xl"], fill=WHITE)
    jq_txt = " · ".join(f"{name}{day}日" for name, day in month_jieqi(today.year, today.month))
    if jq_txt:
        d.text((W - 60, 42), jq_txt, font=f["m"], fill=WHITE, anchor="ra")

    # ---- 星期表头（日红 一~五黑 六蓝）----
    col_w = W / 7
    HEAD_Y = 158
    for i, wd in enumerate(WEEKDAYS):
        color = RED if i == 0 else BLUE if i == 6 else BLACK
        d.text(((i + 0.5) * col_w, HEAD_Y), wd, font=f["l"], fill=color, anchor="ma")

    # ---- 6x7 网格 ----
    GRID_TOP, GRID_BOT = 210, 1100
    row_h = (GRID_BOT - GRID_TOP) / 6
    for i in range(1, 7):    # 竖线
        d.line([(i * col_w, GRID_TOP), (i * col_w, GRID_BOT)], fill=GRID_GRAY, width=2)
    for j in range(1, 6):    # 横线
        d.line([(0, GRID_TOP + j * row_h), (W, GRID_TOP + j * row_h)], fill=GRID_GRAY, width=2)

    grid = month_grid(today.year, today.month)
    for r, week in enumerate(grid):
        row_y = GRID_TOP + r * row_h
        for c, day in enumerate(week):
            cx = (c + 0.5) * col_w
            is_today = day == today.day
            if is_today:
                # 今日：淡黄底格（整格圆角块，避开网格线）
                d.rounded_rectangle(
                    [c * col_w + 7, row_y + 7, (c + 1) * col_w - 7, row_y + row_h - 7],
                    radius=16, fill=TODAY_BG)
            if not day:
                continue
            date = dt.date(today.year, today.month, day)
            # 日号颜色：周日红 / 周六蓝 / 平日黑；今日黑（黄底上最清晰）
            # （month_grid 的 0 占位已跳过非本月日，此处恒为本月）
            if is_today:
                num_color = BLACK
            elif date.weekday() == 6:
                num_color = RED
            elif date.weekday() == 5:
                num_color = BLUE
            else:
                num_color = BLACK
            d.text((cx, row_y + 18), str(day), font=f["num"], fill=num_color, anchor="ma")
            if is_today:
                # 红圈围住日号（挂历红笔圈今日）
                d.ellipse([cx - 44, row_y + 12, cx + 44, row_y + 104],
                          outline=RED, width=6)
            # 小字：节日红 > 节气绿 > 农历黑（初一显月名）
            mark = solar_festival(date) or lunar_festival(date)
            mark_color = RED
            if not mark:
                mark = jieqi(date)
                mark_color = GREEN
            label = mark or lunar_day_label(date)
            color = mark_color if mark else (RED if date.weekday() == 6
                                             else BLUE if date.weekday() == 5 else BLACK)
            d.text((cx, row_y + 108), label, font=f["s"], fill=color, anchor="ma")

    # ---- 底部寄语条（蓝竖条 + 黑句子 + 灰落款）----
    ll = Solar.fromYmd(today.year, today.month, today.day).getLunar()
    quote = daily_quote(today, context=(
        f"农历{ll.getMonthInChinese()}月{ll.getDayInChinese()}，"
        f"今日标注「{calendar_facts.day_mark(today) or '无'}」。"))
    d.rectangle([60, 1122, 70, 1190], fill=BLUE)
    qf = _fit(d, fp, quote, f["l"], W - 380)
    d.text((96, 1128), quote, font=qf, fill=BLACK)
    foot = (f"{today.year}.{today.month:02d}.{today.day:02d} · "
            f"农历{ll.getMonthInChinese()}月{ll.getDayInChinese()}")
    d.text((W - 60, 1160), foot, font=f["s"], fill=FOOT_GRAY, anchor="ra")
    return img


# ═══════════════════════ 对外入口（含缓存） ═══════════════════════

def _prune_cache() -> None:
    """只保留今日缓存（跨天文件名不同，旧的帧+原图一并清掉）。"""
    stem = f"calendar_{dt.date.today():%Y%m%d}"
    for f in CACHE_DIR.glob("calendar_*.fps6"):
        if f.stem != stem:
            try:
                f.unlink(missing_ok=True)
                (CACHE_DIR / f"{f.stem}.png").unlink(missing_ok=True)
            except OSError:
                pass


def render_calendar_card() -> bytes | None:
    """渲染当月日历卡片 → FPS6 字节（当日缓存；纯本地，恒可用）。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"calendar_{dt.date.today():%Y%m%d}"
    cache = CACHE_DIR / f"{stem}.fps6"
    if cache.exists():
        return cache.read_bytes()

    img = render_month()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    prepared = prepare_image(png, dither=True)   # 横图自动旋转为 FPS6 竖屏数据
    try:
        (CACHE_DIR / f"{stem}.png").write_bytes(png)   # 原图：预览缩放来源（ADR-0001）
    except OSError:
        logger.warning("calendar original save failed: %s", stem)
    cache.write_bytes(prepared.data)
    _prune_cache()
    return prepared.data


def calendar_original_png() -> bytes | None:
    """当日日历卡片原图（横屏视角彩色 PNG）字节；未渲染/缺失返回 None。"""
    png = CACHE_DIR / f"calendar_{dt.date.today():%Y%m%d}.png"
    try:
        return png.read_bytes() or None
    except OSError:
        return None


def render_calendar_slot() -> dict | None:
    """对外入口：渲染日历卡片，返回内容清单字段（同 weather，url 由路由层组装）。"""
    data = render_calendar_card()
    if not data:
        return None
    today = dt.date.today()
    content_id = "calendar-" + hashlib.sha256(data).hexdigest()[:10]
    return {
        "id": content_id,
        "filename": "calendar_card",
        "caption": f"日历 · {today.year}年{today.month}月",
        "date": today.strftime("%Y.%m.%d"),
        "memory_score": None,
        "width": DEVICE_WIDTH,
        "height": DEVICE_HEIGHT,
        "landscape": 1,   # 卡片横屏
    }


# ═══════════════════════ 内容源适配器（ADR-0002） ═══════════════════════

class CalendarSource:
    """日历卡片内容源：calendar- 前缀的渲染命名空间（当前内容 = 当月月历）。

    id 是当月渲染帧的内容指纹（设备变更检测用），不是查找键。
    清单字段不含 url/preview_url（源不负责 url，见 ADR-0002）。
    """

    id_prefix = "calendar-"
    missing_detail = "no calendar card"

    def meta(self) -> dict | None:
        return render_calendar_slot()

    def render(self, content_id: str) -> bytes | None:
        return render_calendar_card()

    def original(self, content_id: str) -> bytes | None:
        return calendar_original_png()


SOURCE = CalendarSource()
