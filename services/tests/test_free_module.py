"""free_module 单元测试：模块选择 / 渲染降级（不依赖外部 API / 网络）。

测试前必须在 import app 之前设置临时 DB 路径（沿用 conftest 模式），
并对 settings 的 LLM / 文生图 key 做 monkeypatch，确保走降级路径。
"""
import datetime as dt
import io
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="framedphoto-free-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from app import free_module as fm  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _no_external_apis(monkeypatch, tmp_path):
    """禁用外部 API（LLM / 即梦 / 通用文生图），缓存目录隔离，模块级缓存复位。"""
    monkeypatch.setattr(settings, "zhipu_api_key", "")
    monkeypatch.setattr(settings, "jimeng_access_key", "")
    monkeypatch.setattr(settings, "jimeng_secret_key", "")
    monkeypatch.setattr(settings, "imagegen_url", "")
    monkeypatch.setattr(fm, "CACHE_DIR", tmp_path / "free_cache")
    monkeypatch.setattr(fm, "HISTORY_FILE", tmp_path / "free_history.json")
    monkeypatch.setattr(fm, "free_enabled", lambda: True)
    monkeypatch.setattr(fm, "_cache_fps6", None)   # 防跨测试泄漏
    monkeypatch.setattr(fm, "_cache_ts", 0)
    monkeypatch.setattr(fm, "_cache_key", "")
    yield


def _png_bytes(w=1200, h=1600, color=(128, 128, 128)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_default_modules_shape():
    mods = fm.default_modules()
    assert len(mods) >= 5
    for m in mods:
        assert m["name"].strip() and m["prompt"].strip()
        assert isinstance(m["enabled"], bool)


def test_load_modules_fallback_to_default(monkeypatch):
    monkeypatch.setattr(fm, "load_modules", fm.default_modules)
    mods = fm.load_modules()
    assert mods and all(m["name"] for m in mods)


def test_pick_module_rotates_by_date():
    picked = {fm.pick_module(dt.date(2026, 8, d))["name"] for d in range(1, 8)}
    assert len(picked) > 1  # 每天轮换 → 一周内出现多个模块


def test_pick_module_first_when_no_rotate(monkeypatch):
    monkeypatch.setattr(fm, "free_rotate", lambda: False)
    names = {fm.pick_module(dt.date(2026, 8, d))["name"] for d in range(1, 8)}
    assert len(names) == 1  # 固定第一个启用模块


def test_pick_module_fixed_disabled_module(monkeypatch):
    """时段固定配置的模块即使「未启用」也能选中（enabled 只影响轮换抽奖池）。"""
    mods = fm.default_modules()
    mods[1]["enabled"] = False            # 「读诗」取消启用
    monkeypatch.setattr(fm, "load_modules", lambda: mods)
    picked = fm.pick_module(dt.date(2026, 8, 10), module_name="读诗")
    assert picked is not None and picked["name"] == "读诗"


def test_birthday_days_injects_exact_count():
    """prompt 含出生日期 → 服务端算好天数注入，不让 LLM 自己算（避免算错）。"""
    module = {"name": "宝宝成长记录", "enabled": False, "prompt": (
        "画面中央写\"出生第 N 天\"（咖啡和花生 2026年5月6日出生，"
        "请根据今天日期计算天数）"
    ), "style": ""}
    txt = fm._birthday_days(module, dt.date(2026, 8, 11))
    assert txt is not None and "出生第 98 天" in txt
    # 出生当天 = 第 1 天；无出生日期 → None
    txt0 = fm._birthday_days(module, dt.date(2026, 5, 6))
    assert txt0 is not None and "出生第 1 天" in txt0
    plain = {"name": "读诗", "prompt": "挑一首古诗", "style": ""}
    assert fm._birthday_days(plain, dt.date(2026, 8, 11)) is None
    # 非法日期不崩溃
    bad = {"name": "x", "prompt": "2026年13月40日出生", "style": ""}
    assert fm._birthday_days(bad, dt.date(2026, 8, 11)) is None


def test_pick_module_fixed_missing_falls_back_to_rotate(monkeypatch):
    """固定配置的模块不存在（如被删除）→ 回退轮换启用模块，且只轮换启用的。"""
    mods = fm.default_modules()
    mods[0]["enabled"] = False            # 第一个模块取消启用
    monkeypatch.setattr(fm, "load_modules", lambda: mods)
    monkeypatch.setattr(fm, "free_rotate", lambda: True)
    for d in range(1, 30):
        m = fm.pick_module(dt.date(2026, 8, d), module_name="不存在的模块")
        assert m is not None and m["enabled"] is True
    # 固定模块不受轮换限制：每天都能选中禁用模块
    fixed = fm.pick_module(dt.date(2026, 8, 10), module_name=mods[0]["name"])
    assert fixed is not None and fixed["name"] == mods[0]["name"] and fixed["enabled"] is False


def test_render_text_card_png():
    module = {"name": "宝宝背单词", "enabled": True, "prompt": "x", "style": "y"}
    data = fm._render_text_card(module, "apple", "苹果\nI eat an apple.")
    assert data is not None
    img = Image.open(io.BytesIO(data))
    assert img.size == (1600, 1200)  # 横屏用户视角画布


def test_compose_card_overlays_text():
    module = {"name": "读诗", "enabled": True, "prompt": "x", "style": "y"}
    data = fm._compose_card(_png_bytes(1728, 1296), module["name"], "静夜思", "举头望明月\n低头思故乡")
    assert data is not None
    img = Image.open(io.BytesIO(data))
    assert img.size == (1600, 1200)


def test_render_free_fps6_full_degrades_to_text_card():
    """无 LLM key + 无文生图 key → 纯文字卡片（FPS6 仍可用）。"""
    data = fm.render_free_fps6(refresh=True)
    assert data is not None
    assert data[:4] == b"FPS6"
    assert len(data) == 20 + 1200 * 1600 // 2


def test_render_free_slot_meta():
    meta = fm.render_free_slot()
    assert meta is not None
    assert meta["id"].startswith("free-")
    assert meta["url"] == "/api/images/free/raw"
    assert meta["preview_url"] == "/api/images/free/preview"
    assert meta["caption"]  # 模块名


# ---------- 跨午夜时段：0 点不换图（同一连续 free 时段复用同一张卡） ----------

def test_cache_file_name_has_segment_start():
    module = {"name": "历史故事", "enabled": True, "prompt": "x", "style": "y"}
    p = fm._cache_file(module, dt.datetime(2026, 8, 5, 22, 2))
    assert "202608052202" in p.name
    assert p.name.startswith("free_")


def test_content_base_falls_back_to_midnight(monkeypatch):
    """非 free 时段（管理台预览等）回退自然日 0 点。"""
    monkeypatch.setattr("app.slots.segment_start", lambda **kw: None)
    base = fm._content_base(None)
    assert base.hour == 0 and base.minute == 0
    assert base.date() == dt.date.today()


def test_free_reuses_card_inside_same_segment(monkeypatch):
    """同一连续 free 时段内（含跨 0 点）复用同一张卡：只调用一次 LLM。"""
    calls = {"n": 0}

    def fake_llm(module, today):
        calls["n"] += 1
        return {"title": "T", "body": "B", "image_prompt": "p"}

    monkeypatch.setattr(fm, "_llm_content", fake_llm)
    monkeypatch.setattr(fm, "_generate_illustration", lambda prompt: _png_bytes())
    base = dt.datetime(2026, 8, 5, 22, 2)   # 段开始固定（0 点前后归属一致）
    monkeypatch.setattr(fm, "_content_base", lambda module_name=None: base)

    d1 = fm.render_free_fps6(module_name="历史故事")
    d2 = fm.render_free_fps6(module_name="历史故事")
    assert d1 == d2
    assert calls["n"] == 1


def test_free_regenerates_when_segment_changes(monkeypatch):
    """进入新的模块时段（归属时刻变化）→ 重新生成新卡。"""
    calls = {"n": 0}

    def fake_llm(module, today):
        calls["n"] += 1
        return {"title": f"T{calls['n']}", "body": "B", "image_prompt": "p"}

    monkeypatch.setattr(fm, "_llm_content", fake_llm)
    monkeypatch.setattr(fm, "_generate_illustration", lambda prompt: _png_bytes())
    monkeypatch.setattr(fm, "_content_base",
                        lambda module_name=None: dt.datetime(2026, 8, 5, 22, 2))
    fm.render_free_fps6(module_name="历史故事")
    monkeypatch.setattr(fm, "_content_base",
                        lambda module_name=None: dt.datetime(2026, 8, 6, 22, 2))
    fm.render_free_fps6(module_name="历史故事")
    assert calls["n"] == 2


def test_render_free_saves_card_png_to_library(monkeypatch):
    """渲染成功后「加文字后的完整卡片 PNG 原图」自动入库（不裁剪/不量化）。"""
    from app import db
    from pathlib import Path
    from app.config import settings

    def fake_llm(module, today):
        return {"title": "T", "body": "B", "image_prompt": "p"}

    def fake_illus(prompt):
        buf = io.BytesIO()
        Image.new("RGB", (1024, 768), (200, 220, 180)).save(buf, "PNG")
        return buf.getvalue()

    monkeypatch.setattr(fm, "_llm_content", fake_llm)
    monkeypatch.setattr(fm, "_generate_illustration", fake_illus)
    before = len(db.list_images())
    assert fm.render_free_fps6(refresh=True) is not None
    items = db.list_images()
    assert len(items) == before + 1
    m = items[0]
    assert m["filename"].startswith("自由模块·")
    assert not m["id"].startswith("free-")          # 避免 raw 转发分支冲突
    up = Path(settings.upload_dir)
    assert (up / f"{m['id']}.orig").exists()        # 原图已保存
    orig = Image.open(io.BytesIO((up / f"{m['id']}.orig").read_bytes()))
    assert orig.size == (1600, 1200)                # 加文字后的完整卡片（非插画原图）
    assert m["landscape"] == 1                       # 横屏卡片
    assert (up / f"{m['id']}.fps6").exists()        # 设备格式副本也已保存

    # 命中缓存时不重复入库
    n2 = len(db.list_images())
    fm.render_free_fps6()
    assert len(db.list_images()) == n2


def test_detect_tiling_flags_bad_and_accepts_good():
    """平铺自检：构造坏数据（重复小块）应判异常，正常数据应通过。"""
    import struct
    from app.epd_image import prepare_image
    from app.free_module import _detect_tiling

    # 正常：随机彩色图
    good_img = Image.new("RGB", (1600, 1200))
    px = good_img.load()
    import random
    rnd = random.Random(42)
    for y in range(1200):
        for x in range(1600):
            px[x, y] = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
    buf = io.BytesIO(); good_img.save(buf, "PNG")
    good = prepare_image(buf.getvalue(), dither=False).data
    assert _detect_tiling(good) is False, "正常图不应判异常"

    # 坏：把一张小图平铺成 4x3 网格（模拟 API 坏图）
    small = good_img.resize((400, 400))
    bad_img = Image.new("RGB", (1600, 1200))
    bpx = bad_img.load()
    for y in range(1200):
        for x in range(1600):
            bpx[x, y] = small.getpixel((x % 400, y % 400))
    buf2 = io.BytesIO(); bad_img.save(buf2, "PNG")
    bad = prepare_image(buf2.getvalue(), dither=False).data
    assert _detect_tiling(bad) is True, "平铺坏图应判异常"
