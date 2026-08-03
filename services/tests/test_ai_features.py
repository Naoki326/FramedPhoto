"""AI 分析 / 每日精选 / 文案渲染 测试。"""
import datetime as dt
import io
import os
import tempfile

import pytest
from PIL import Image

from app import db, daily
from app.analyzer import (
    VLMUnavailable,
    _call_vlm,
    analyze_image,
    extract_exif,
    heuristic_score,
)
from app.epd_image import prepare_image_with_caption

_tmp = tempfile.mkdtemp(prefix="framedphoto-ai-test-")


# ---------- 启发式评分 ----------

def _make_photo(path: str, size=(1200, 1600), color=(120, 90, 200)) -> str:
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")
    return path


def test_heuristic_score_basic():
    p = _make_photo(os.path.join(_tmp, "basic.png"))
    mem, beauty, reason = heuristic_score(p)
    assert 0 <= mem <= 100 and 0 <= beauty <= 100
    assert reason


def test_heuristic_invalid_file():
    p = os.path.join(_tmp, "bad.png")
    with open(p, "w") as f:
        f.write("not an image")
    mem, beauty, _ = heuristic_score(p)
    assert 0 <= mem <= 100 and 0 <= beauty <= 100  # 不抛异常


def test_extract_exif_time():
    # PIL 写 EXIF 拍摄时间
    img = Image.new("RGB", (10, 10))
    exif = Image.Exif()
    exif[0x0132] = "2023:05:14 16:30:22"
    p = os.path.join(_tmp, "exif.png")
    img.save(p, exif=exif)
    shot_at, lat, lon, source = extract_exif(p)
    assert shot_at is not None
    assert source == "exif"
    t = dt.datetime.fromtimestamp(shot_at, tz=dt.timezone.utc)
    assert (t.year, t.month, t.day) == (2023, 5, 14)
    assert lat is None and lon is None


def test_extract_exif_fallback_mtime():
    p = _make_photo(os.path.join(_tmp, "noexif.png"))
    shot_at, _, _, source = extract_exif(p)
    assert shot_at is not None  # 兜底用文件 mtime
    assert source == "file"


# ---------- VLM 调用 ----------

def test_vlm_unavailable_without_config():
    with pytest.raises(VLMUnavailable):
        _call_vlm(b"fake-image-bytes")


def test_analyze_image_heuristic_when_vlm_disabled(monkeypatch):
    p = _make_photo(os.path.join(_tmp, "hv.png"))
    monkeypatch.setattr("app.analyzer.settings.vlm_enabled", False)
    a = analyze_image(p, use_vlm=False)
    assert a.source == "heuristic"
    assert a.path == p
    assert 0 <= a.memory_score <= 100
    assert a.filename == "hv.png"


def test_analyze_image_vlm_fallback(monkeypatch):
    """VLM 失败时降级启发式，不抛异常。"""
    p = _make_photo(os.path.join(_tmp, "vf.png"))
    monkeypatch.setattr("app.analyzer.settings.vlm_enabled", True)
    monkeypatch.setattr("app.analyzer.settings.vlm_api_url", "http://127.0.0.1:1/v1")
    a = analyze_image(p, use_vlm=True)
    assert a.source == "heuristic"
    assert "VLM 不可用" in a.reason


def test_analyze_image_vlm_success(monkeypatch):
    p = _make_photo(os.path.join(_tmp, "vs.png"))
    monkeypatch.setattr("app.analyzer.settings.vlm_enabled", True)

    def fake_call(raw):
        return {
            "description": "海边日落",
            "type": "风景",
            "memory_score": 88,
            "beauty_score": 90,
            "caption": "那天傍晚的海",
            "reason": "光影很美",
        }
    monkeypatch.setattr("app.analyzer._call_vlm", fake_call)
    a = analyze_image(p, use_vlm=True)
    assert a.source == "vlm"
    assert a.memory_score == 88
    assert a.caption == "那天傍晚的海"


# ---------- 文案渲染 ----------

def test_prepare_image_with_caption_layout():
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), (200, 100, 50)).save(buf, format="PNG")
    p = prepare_image_with_caption(buf.getvalue(), caption="纪念我们一起旅行的日子", date_str="2023.05.14")
    assert p.width == 1200 and p.height == 1600
    assert len(p.data) == 20 + 960000
    # 底部文案区应比纯照片版本更暗（渐变带）
    p2 = prepare_image_with_caption(buf.getvalue(), caption="", date_str="")
    bottom = p.data[20 + 1599 * 600:20 + 1600 * 600]
    bottom2 = p2.data[20 + 1599 * 600:20 + 1600 * 600]
    assert bottom != bottom2  # 底部有文字/渐变覆盖


def test_prepare_image_with_caption_empty_is_plain():
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), (200, 100, 50)).save(buf, format="PNG")
    a = prepare_image_with_caption(buf.getvalue(), caption="", date_str="")
    from app.epd_image import prepare_image
    b = prepare_image(buf.getvalue(), dither=True)
    assert a.data == b.data  # 无文案时行为与纯照片一致


# ---------- 每日精选 ----------

def _seed_scores():
    # 构造两张评分照片：一张拍摄于"历史上的今天"（假设今天 8 月 3 日附近）
    today = dt.date.today()
    p1 = _make_photo(os.path.join(_tmp, "daily1.png"))
    p2 = _make_photo(os.path.join(_tmp, "daily2.png"))
    shot = dt.datetime(today.year - 2, today.month, today.day, 12, 0, 0)
    db.upsert_photo_score(p1, filename="daily1.png", memory_score=90,
                          shot_at=shot.timestamp(), shot_source="exif", analyzed_at=db.now())
    db.upsert_photo_score(p2, filename="daily2.png", memory_score=40,
                          shot_at=shot.timestamp(), shot_source="exif", analyzed_at=db.now())
    return p1, p2


def test_select_daily_photo_historical_today(monkeypatch):
    _seed_scores()
    monkeypatch.setattr("app.daily.settings.daily_min_score", 50)
    chosen = daily.select_daily_photo()
    assert chosen is not None
    assert chosen["memory_score"] >= 50  # 高于阈值的候选
    assert chosen["filename"] == "daily1.png"


def test_select_daily_falls_back_to_high_score(monkeypatch):
    """无历史上的今天候选时，退化为全局高分。"""
    p = _make_photo(os.path.join(_tmp, "high.png"))
    # 拍摄日期故意设为"非今天"
    shot = dt.datetime(2020, 1, 1, 12, 0, 0)
    db.upsert_photo_score(p, filename="high.png", memory_score=95,
                          shot_at=shot.timestamp(), shot_source="exif", analyzed_at=db.now())
    monkeypatch.setattr("app.daily.settings.daily_min_score", 99)  # 阈值过高 → 无历史候选
    chosen = daily.select_daily_photo()
    assert chosen is not None


def test_render_daily_creates_file():
    _seed_scores()
    meta = daily.render_daily()
    assert meta is not None
    assert daily.daily_fps6_exists()
    assert daily.daily_is_fresh()
    assert meta["id"] == "daily"
    assert meta["width"] == 1200
