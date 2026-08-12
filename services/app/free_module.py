"""free_module.py — 自由模块卡片（替代原「新闻卡片」时段）。

核心思路：管理台配置一段「模块 prompt」（如：宝宝背单词、读诗、历史故事、
李白的诗和酒、童话故事……），由 LLM（智谱 GLM）根据该 prompt 生成当前时段
具体内容（标题 / 正文 / 画面描述），即梦文生图出插画，PIL 叠加文字渲染成
FPS6 卡片。每个模块时段生成一次，时段内保持同一张卡（跨 0 点的连续同模块
时段也不换图）；进入新的模块时段才重新生成。多个模块时可每天轮换。

配置（runtime_config.json，管理台可编辑）：
    free_enabled: bool         自由模块时段开关
    free_rotate: bool          每天轮换启用中的模块（false = 固定第一个）
    free_modules: [            模块列表
        {"name": "宝宝背单词", "enabled": true,
         "prompt": "……给 LLM 的主题描述（想看到什么内容）……",
         "style": "……插画风格描述……"}
    ]

降级链：LLM 失败 → 纯插画（标题用模块名）；文生图失败 → 纯文字卡片；
两者都不可用 → None（回退照片时段）。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import logging
import struct
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.epd_image import (
    DEVICE_HEIGHT,
    DEVICE_WIDTH,
    PreparedImage,
    SPECTRA6_PALETTE,
    find_cjk_font,
    prepare_image,
    preview_png_landscape,
)

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / "free_cache"
CACHE_DIR.mkdir(exist_ok=True)

# ═══════════════════════ 内置模块 ═══════════════════════

DEFAULT_MODULES: list[dict] = [
    {
        "name": "宝宝背单词",
        "enabled": True,
        "prompt": "为 3-6 岁宝宝挑选一个简单常用的英文单词：标题写单词本身，正文写中文释义加一句简单例句，画面是可爱的字母/动物/物品卡通插画。",
        "style": "温馨可爱的儿童卡通插画，扁平、暖色调、圆润，色彩鲜艳",
    },
    {
        "name": "读诗",
        "enabled": True,
        "prompt": "挑一首脍炙人口的中国古诗：标题写诗名，正文写最有名的一联诗句，画面是诗句意境的国风插画。",
        "style": "中国水墨国风插画，意境悠远，淡雅配色",
    },
    {
        "name": "历史故事",
        "enabled": True,
        "prompt": "讲一个简短有趣的中国历史小故事：标题写故事名，正文用一两句话概括故事，画面是故事场景插画。",
        "style": "写实绘本插画，细节丰富，色彩沉稳",
    },
    {
        "name": "李白的诗和酒",
        "enabled": True,
        "prompt": "从李白的诗中挑一句与酒有关的：标题写诗名，正文写这句诗，画面是李白月下独酌、诗酒意境的古风插画。",
        "style": "古风插画，水墨与金色结合，意境悠远",
    },
    {
        "name": "童话故事",
        "enabled": True,
        "prompt": "讲一个经典童话：标题写故事名，正文用一两句话概括最动人的场景，画面是童话场景插画。",
        "style": "梦幻童话插画，色彩柔和浪漫",
    },
    {
        "name": "今日新闻",
        "enabled": False,
        "prompt": "挑选今天一条有代表性、适合家庭阅读的新闻：标题用一句话概括新闻，正文补充一两句要点，画面是新闻主题的卡通插画。",
        "style": "温馨卡通插画，扁平、暖色调",
    },
]


def default_modules() -> list[dict]:
    """内置模块的深拷贝（避免外部修改污染默认值）。"""
    return json.loads(json.dumps(DEFAULT_MODULES))


def load_modules() -> list[dict]:
    """运行时配置的模块列表；未配置/为空 → 内置模块。"""
    from app.runtime_config import get
    mods = get("free_modules")
    if not isinstance(mods, list) or not mods:
        return default_modules()
    out = []
    for m in mods:
        if not isinstance(m, dict) or not str(m.get("name", "")).strip():
            continue
        out.append({
            "name": str(m["name"]).strip(),
            "enabled": bool(m.get("enabled", True)),
            "prompt": str(m.get("prompt", "")).strip(),
            "style": str(m.get("style", "")).strip(),
        })
    return out or default_modules()


def enabled_modules() -> list[dict]:
    return [m for m in load_modules() if m["enabled"]]


def free_enabled() -> bool:
    from app.runtime_config import effective
    return bool(effective("free_enabled", settings))


def free_rotate() -> bool:
    from app.runtime_config import effective
    return bool(effective("free_rotate", settings))


def pick_module(today: dt.date | None = None, module_name: str | None = None) -> dict | None:
    """当天模块。

    module_name 指定时固定返回该模块（时段块固定配置；不受「启用」开关影响，
    已禁用也可在时段中固定选用；不存在才回退轮换）；
    否则每天轮换启用中的模块（同一天稳定）；free_rotate=false 固定第一个。
    """
    today = today or dt.date.today()
    if module_name:
        for m in load_modules():
            if m["name"] == module_name:
                return m
    mods = enabled_modules()
    if not mods:
        return None
    if free_rotate() and len(mods) > 1:
        return mods[today.toordinal() % len(mods)]
    return mods[0]


# ═══════════════════════ LLM 生成当天内容 ═══════════════════════

_LLM_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

_LLM_SYSTEM = (
    "你是电子纸相框的每日内容生成器。根据用户提供的「模块主题」，为今天生成一段"
    "适合展示在墨水屏上的内容。只输出 JSON，不要任何其他文字或解释。JSON 形如：\n"
    '{"title": "大字标题（≤8 个汉字或英文单词）",\n'
    ' "body": "正文，1-3 行，每行不超过 14 字，总长不超过 40 字，简体中文",\n'
    ' "image_prompt": "文生图画面描述（40-70 字，只描述画面，禁止出现任何文字、字母、水印）"}\n'
    "要求：\n"
    "- 内容每天不同：结合今天的日期、季节、节气随机挑选；\n"
    "- 用户会给出「最近已展示过的内容」，必须从中避让，选择不同的新内容；\n"
    "- title 是核心内容（单词、诗名、故事名、新闻一句话等）；\n"
    "- body 是释义 / 诗句 / 故事梗概（背单词类可含英文单词+中文释义，诗类写诗句原文）；\n"
    "- image_prompt 只描述画面，绝不能包含任何文字、字母、数字、水印。"
)

# 生成历史记录（模块 → 最近标题列表），用于注入 LLM 避免重复
HISTORY_FILE = CACHE_DIR / "free_history.json"


def _load_history() -> dict:
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _record_title(module_name: str, title: str) -> None:
    """记录本次生成的标题（每个模块保留最近 20 个）。"""
    if not title:
        return
    try:
        h = _load_history()
        lst = h.setdefault(module_name, [])
        lst.append(title)
        h[module_name] = lst[-20:]
        HISTORY_FILE.write_text(json.dumps(h, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    except OSError:
        pass


def _recent_titles(module_name: str, n: int = 8) -> list[str]:
    """该模块最近生成的标题（不含今天的，供避让）。"""
    return _load_history().get(module_name, [])[-n:]


def _birthday_days(module: dict, today: dt.date) -> str | None:
    """若模块 prompt 含「YYYY年M月D日出生」，计算今天是出生第几天。

    返回追加给 LLM 的指令文本（含算好的天数，不让 LLM 自己做算术），
    无出生日期返回 None。
    """
    import re
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*出生", module.get("prompt", ""))
    if not m:
        return None
    try:
        born = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    days = (today - born).days + 1   # 出生当天算第 1 天
    if days < 1:
        return None
    return (f"宝宝出生日期：{born.year}年{born.month}月{born.day}日（今天出生第 {days} 天）。"
            f"画面中的天数文字必须用『出生第 {days} 天』，不许自己计算或改写。")


def _llm_content(module: dict, today: dt.date) -> dict | None:
    """调智谱 GLM 生成当天内容 {title, body, image_prompt}。无 key/失败返回 None。"""
    key = settings.zhipu_api_key
    if not key:
        return None
    recent = _recent_titles(module["name"])
    avoid = ""
    if recent:
        avoid = (f"最近已展示过的内容（必须避让，选择不同的）："
                 f"{'、'.join(recent)}。\n")
    birthday = _birthday_days(module, today)
    birthday_line = birthday + "\n" if birthday else ""
    user = (f"今天是 {today.year}年{today.month}月{today.day}日。\n"
            f"模块主题：{module['prompt']}\n"
            f"{birthday_line}"
            f"{avoid}"
            f"插画风格：{module.get('style', '')}\n"
            "只输出 JSON。")
    try:
        r = requests.post(
            _LLM_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": settings.zhipu_model,
                "messages": [
                    {"role": "system", "content": _LLM_SYSTEM},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.9,
                "max_tokens": 500,
            },
            timeout=30,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(content[start:end + 1])
        out = {
            "title": str(data.get("title", "")).strip()[:20],
            "body": str(data.get("body", "")).strip()[:60],
            "image_prompt": str(data.get("image_prompt", "")).strip()[:200],
        }
        if not out["title"] and not out["body"]:
            return None
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("free llm content failed: %s", exc.__class__.__name__)
        return None


# ═══════════════════════ 文生图 ═══════════════════════

def _generate_cartoon(prompt: str) -> bytes | None:
    """通用 OpenAI 图片接口降级（未配即梦时）。返回 PNG 字节或 None。"""
    if not (settings.imagegen_url and settings.imagegen_key and settings.imagegen_model):
        return None
    try:
        r = requests.post(
            settings.imagegen_url,
            headers={"Authorization": f"Bearer {settings.imagegen_key}"},
            json={"model": settings.imagegen_model, "prompt": prompt,
                  "n": 1, "size": "1536x1024", "response_format": "b64_json"},
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


def _generate_illustration(image_prompt: str) -> bytes | None:
    """生成插画：优先即梦 4.6，其次通用图片接口。返回原始图片字节或 None。"""
    from app.jimeng import generate_image
    raw = generate_image(image_prompt)
    if raw:
        return raw
    if settings.imagegen_url:
        return _generate_cartoon(image_prompt)
    return None


# ═══════════════════════ 渲染 ═══════════════════════

def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    src_w, src_h = image.size
    scale = max(width / src_w, height / src_h)
    img = image.resize((round(src_w * scale), round(src_h * scale)), Image.LANCZOS)
    left = (img.width - width) // 2
    top = (img.height - height) // 2
    return img.crop((left, top, left + width, top + height))


def _fit_title_font(draw: ImageDraw.ImageDraw, font_path: str, text: str,
                    max_w: int, start: int = 64, minimum: int = 34) -> ImageFont.FreeTypeFont:
    """让标题在 max_w 内一行放下（自动缩小字号；多行取首行）。"""
    text = text.split("\n", 1)[0]
    size = start
    while size > minimum:
        f = ImageFont.truetype(font_path, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return ImageFont.truetype(font_path, minimum)


def _wrap_lines(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """按像素宽度换行（\n 视为强制换行）。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        trial = cur + ch
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _compose_card(illustration: bytes, module_name: str, title: str, body: str) -> bytes | None:
    """插画铺底 + 顶部标题衬底块 + 底部正文衬底块 → 1600x1200 横屏视角 PNG。

    最终整幅交给 prepare_image 旋转 90° 变为 FPS6 竖屏数据，相框横放观看即正立。
    """
    try:
        img = Image.open(io.BytesIO(illustration)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning("free illustration decode failed: %s", exc)
        return None

    W, H = DEVICE_HEIGHT, DEVICE_WIDTH  # 1600 x 1200（横屏用户视角）
    img = _cover(img, W, H)
    draw = ImageDraw.Draw(img)
    font_path = find_cjk_font()
    if not font_path:
        # 无中文字体：退化为纯插画
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # ---- 文字衬底：不遮挡插画，仅给文字加小块圆角深底保证可读 ----
    BAND = (22, 22, 26)      # 深色衬底（E Ink 近似黑）
    PAD = 22                 # 文字与衬底内边距
    CORNER = 22              # 圆角半径

    # 顶部：模块名 + 标题合成一个自适应圆角块
    f_mod = ImageFont.truetype(font_path, 30)
    top_rows = [(f_mod, module_name)]
    if title:
        tfont = _fit_title_font(draw, font_path, title, W - 200)  # 留内边距
        top_rows.append((tfont, title))
    tw = max((draw.textlength(t, font=f) for f, t in top_rows), default=0)
    th = sum(f.size for f, _ in top_rows) + 14 * (len(top_rows) - 1)
    bx0, by0 = 56, 56
    draw.rounded_rectangle([bx0, by0, bx0 + tw + 2 * PAD, by0 + th + 2 * PAD],
                           radius=CORNER, fill=BAND)
    ty = by0 + PAD
    for f, t in top_rows:
        draw.text((bx0 + PAD, ty), t, font=f, fill=(255, 255, 255))
        ty += f.size + 14

    # 底部：正文行合成一个自适应的圆角块（居中）
    if body:
        bfont = ImageFont.truetype(font_path, 40)
        lines = _wrap_lines(draw, body, bfont, W - 220)[:3]
        if lines:
            bw = max(draw.textlength(l, bfont) for l in lines)
            bh = len(lines) * 64
            bbx = (W - bw) / 2 - PAD
            bby = H - bh - PAD * 2 - 56
            draw.rounded_rectangle([bbx, bby, bbx + bw + 2 * PAD, bby + bh + 2 * PAD],
                                   radius=CORNER, fill=BAND)
            ty = bby + PAD
            for ln in lines:
                lw = draw.textlength(ln, font=bfont)
                draw.text(((W - lw) / 2, ty), ln, font=bfont, fill=(255, 255, 255))
                ty += 64

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_text_card(module: dict, title: str, body: str) -> bytes | None:
    """降级：纯文字卡片（暖色背景 + 圆角卡片），无中文字体返回 None。"""
    font_path = find_cjk_font()
    if not font_path:
        return None
    W, H = DEVICE_HEIGHT, DEVICE_WIDTH
    img = Image.new("RGB", (W, H), (255, 247, 230))
    draw = ImageDraw.Draw(img)
    black, orange, gray = (60, 40, 20), (240, 120, 40), (130, 110, 90)

    draw.text((56, 44), f"✦ {module['name']}",
              font=ImageFont.truetype(font_path, 40), fill=orange)
    if title:
        tfont = _fit_title_font(draw, font_path, title, W - 112, start=60, minimum=34)
        draw.text((56, 116), title, font=tfont, fill=black)
    if body:
        bfont = ImageFont.truetype(font_path, 36)
        lines = _wrap_lines(draw, body, bfont, W - 112)[:4]
        y = 240
        for ln in lines:
            draw.text((56, y), ln, font=bfont, fill=black)
            y += 54
    draw.text((56, H - 60), "自由模块 · LLM 每日生成",
              font=ImageFont.truetype(font_path, 24), fill=gray)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ═══════════════════════ 生成后自检（防坏图） ═══════════════════════

# 平铺检测阈值：正常插画行自相关得分通常 >140；坏图（API 偶发返回
# 重复平铺/错乱插画）得分显著低于 60。取 100 留有充分余量。
TILE_SCORE_OK = 100.0


def _detect_tiling(data: bytes) -> bool:
    """检测 FPS6 内容是否重复平铺（生成异常特征）。True = 异常，应丢弃重试。

    原理：正常插画相邻内容差异大；平铺异常时以 400px/800px 偏移的行自相关
    极低（同图重复）。返回 False 表示内容正常。
    """
    try:
        w, h = struct.unpack_from("<II", data, 4)
        if (w, h) != (DEVICE_WIDTH, DEVICE_HEIGHT) or len(data) != 20 + w * h // 2:
            return True
        prepared = PreparedImage(width=w, height=h, data=data,
                                 palette=SPECTRA6_PALETTE)
        img = Image.open(io.BytesIO(preview_png_landscape(prepared, True))).convert("RGB")
        W, H = img.size
        px = img.load()
        vals = []
        for yy in range(80, H - 80, 60):
            row = [sum(px[x, yy][c] for c in range(3)) for x in range(W)]
            s400 = sum(abs(row[x] - row[x + 400]) for x in range(0, W - 400, 8)) / ((W - 400) // 8)
            s800 = sum(abs(row[x] - row[x + 800]) for x in range(0, W - 800, 8)) / ((W - 800) // 8)
            vals.append(min(s400, s800))
        if not vals:
            return True
        return (sum(vals) / len(vals)) < TILE_SCORE_OK
    except Exception as exc:  # noqa: BLE001
        logger.warning("free tiling check failed: %s", exc)
        return True   # 解析失败视为异常，走重试/降级


# ═══════════════════════ 归档到内容库 ═══════════════════════

def _save_to_library(module_name: str, png: bytes) -> None:
    """把加文字后的完整卡片 PNG 原图自动保存到内容库。

    - 用普通 uuid 作 id（避免 free- 前缀与 raw 转发分支冲突）
    - orig 保存原始 PNG（不裁剪 / 不 6 色量化 / 不转 FPS6），fps6 为设备格式副本
    - landscape=1（卡片为横屏视角）
    - 失败仅记日志，不影响渲染主流程
    """
    try:
        import uuid
        from pathlib import Path
        from app import db
        from app.config import settings
        from app.epd_image import prepare_image
        prepared = prepare_image(png, dither=True)
        img_id = uuid.uuid4().hex[:12]
        up = Path(settings.upload_dir)
        up.mkdir(parents=True, exist_ok=True)
        (up / f"{img_id}.orig").write_bytes(png)          # 加文字的原图（PNG 原样）
        (up / f"{img_id}.fps6").write_bytes(prepared.data)
        db.insert_image(img_id, f"自由模块·{module_name}", f"{img_id}.fps6",
                        prepared.width, prepared.height, landscape=1)
        logger.info("自由模块卡片原图已保存到内容库: %s (%s)", module_name, img_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自由模块卡片原图入库失败: %s", exc)


# ═══════════════════════ 对外入口（含缓存） ═══════════════════════

# 生成后 12 小时内复用（与旧新闻卡片一致：避免 0 点边界刚生成立刻重生成）
FREE_CACHE_TTL = 12 * 3600

_cache_fps6: bytes | None = None
_cache_ts: float = 0
_cache_key: str = ""


def _cache_file(module: dict, base: dt.datetime) -> Path:
    digest = hashlib.sha256(
        json.dumps(module, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:8]
    return CACHE_DIR / f"free_{base:%Y%m%d%H%M}_{digest}.fps6"


def _content_base(module_name: str | None) -> dt.datetime:
    """内容归属时刻：当前 free 连续段（type+module 一致，可跨午夜）的开始。

    0 点前后若为同一模块时段（如 22:02-24:00 与 00:00-04:30 都是「历史故事」），
    归属时刻不变 → 复用同一张卡，0 点不换图；进入新的模块时段 → 归属时刻
    更新 → 重新生成。非 free 时段（管理台预览等）回退自然日 0 点。
    """
    from app import slots
    seg_start = slots.segment_start(typ=slots.SLOT_FREE, module=module_name)
    if seg_start is not None:
        return seg_start
    return dt.datetime.combine(dt.date.today(), dt.time(0, 0))


def render_free_fps6(refresh: bool = False, module_name: str | None = None) -> bytes | None:
    """渲染今日自由模块卡片 → FPS6 字节；未启用/不可用返回 None。

    refresh=True 强制重生成（跳过缓存，管理台预览用）。
    module_name 指定时固定使用该模块（时段块固定配置）。
    """
    global _cache_fps6, _cache_ts, _cache_key
    if not free_enabled():
        return None
    module = pick_module(module_name=module_name)
    if not module:
        return None
    base = _content_base(module_name)
    cache_file = _cache_file(module, base)
    cache_key = f"{base:%Y%m%d%H%M}-{cache_file.name}"
    now = dt.datetime.now().timestamp()

    if not refresh:
        # 内存缓存
        if _cache_fps6 and _cache_key == cache_key and (now - _cache_ts) < FREE_CACHE_TTL:
            return _cache_fps6
        # 文件缓存（服务重启后 12h 内复用）
        if cache_file.exists() and (now - cache_file.stat().st_mtime) < FREE_CACHE_TTL:
            data = cache_file.read_bytes()
            _cache_fps6, _cache_ts, _cache_key = data, now, cache_key
            return data

    content = _llm_content(module, base.date())
    if content and content.get("title"):
        _record_title(module["name"], content["title"])
    title = (content or {}).get("title") or module["name"]
    body = (content or {}).get("body") or ""
    image_prompt = ((content or {}).get("image_prompt")
                    or f"{module.get('style', '温馨插画')}，主题：{module['prompt']}")

    data = None
    # 生成 + 平铺自检；异常时重试一次（重新调即梦出图），仍异常走文字卡降级
    for attempt in range(2):
        raw = _generate_illustration(image_prompt)
        png = _compose_card(raw, module["name"], title, body) if raw else None
        if not png:
            break
        data = prepare_image(png, dither=True).data
        if _detect_tiling(data):
            logger.warning("free card tiling detected, retry %d: %s", attempt + 1, module["name"])
            data = None
            continue
        _save_to_library(module["name"], png)   # 自检通过才入库
        break

    if not data:
        # 纯文字卡片（PNG → FPS6）降级
        png = _render_text_card(module, title, body)
        if png:
            data = prepare_image(png, dither=True).data
    if not data:
        logger.warning("free module render failed: %s", module["name"])
        return None

    try:
        cache_file.write_bytes(data)
    except OSError:
        pass
    _cache_fps6, _cache_ts, _cache_key = data, now, cache_key
    return data


def render_free_slot(module_name: str | None = None) -> dict | None:
    """对外入口：渲染自由模块卡片 → content 元数据；不可用返回 None。

    module_name 为时段块固定配置的模块名；None 则按轮换逻辑。
    """
    data = render_free_fps6(module_name=module_name)
    if not data:
        return None
    module = pick_module(module_name=module_name)
    name = module["name"] if module else "自由模块"
    base = _content_base(module_name)
    content_id = "free-" + hashlib.sha256(data).hexdigest()[:10]
    return {
        "id": content_id,
        "filename": "free_module",
        "caption": name,
        "date": base.strftime("%Y.%m.%d"),
        "width": DEVICE_WIDTH,
        "height": DEVICE_HEIGHT,
        "url": "/api/images/free/raw",
        "preview_url": "/api/images/free/preview",
    }
