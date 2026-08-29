"""calendar_card 测试：月历数据 / 渲染冒烟 / 每日寄语降级 / 内容源接入。"""
import datetime as dt
import json

import pytest

from app import calendar_card, calendar_facts
from app.epd_image import parse_fps6


# ---------- 月历数据 ----------

def test_month_grid_shape_sunday_first():
    """6x7 矩阵、周日开头；2026-08-01 是周六 → 第一行第 7 列。"""
    grid = calendar_card.month_grid(2026, 8)
    assert len(grid) == 6
    assert all(len(row) == 7 for row in grid)
    assert grid[0] == [0, 0, 0, 0, 0, 0, 1]
    assert 31 in grid[5]          # 8/31 在第 6 周


def test_month_grid_padded_to_six_weeks():
    """不足 6 周的月份补空行，网格行高稳定。"""
    grid = calendar_card.month_grid(2026, 2)   # 2026-02 恰 4 周（2/1 周日）
    assert len(grid) == 6
    assert all(day == 0 for day in grid[5])


def test_month_jieqi_finds_august_terms():
    marks = calendar_card.month_jieqi(2026, 8)
    names = [name for name, _ in marks]
    assert "立秋" in names and "处暑" in names


# ---------- 渲染冒烟 ----------

def test_render_month_size():
    img = calendar_card.render_month(dt.date(2026, 8, 5))
    assert img.size == (1600, 1200)   # 横屏用户视角


def test_render_calendar_card_fps6(monkeypatch):
    monkeypatch.setattr("app.config.settings.zhipu_api_key", "")   # 寄语走降级
    data = calendar_card.render_calendar_card()
    assert data
    prepared = parse_fps6(data)          # magic / 长度校验
    assert (prepared.width, prepared.height) == (1200, 1600)
    # 当日缓存命中（第二次调用读缓存，字节一致）
    assert calendar_card.render_calendar_card() == data


def test_render_calendar_slot_fields(monkeypatch):
    monkeypatch.setattr("app.config.settings.zhipu_api_key", "")
    meta = calendar_card.render_calendar_slot()
    assert meta
    assert meta["id"].startswith("calendar-")
    assert meta["landscape"] == 1
    assert "日历" in meta["caption"]


def test_calendar_original_png_roundtrip(monkeypatch):
    monkeypatch.setattr("app.config.settings.zhipu_api_key", "")
    assert calendar_card.render_calendar_card()
    png = calendar_card.calendar_original_png()
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


# ---------- 每日寄语 ----------

def test_quote_fallback_stable_without_llm(monkeypatch):
    monkeypatch.setattr("app.config.settings.zhipu_api_key", "")
    today = dt.date(2026, 8, 5)
    q1 = calendar_card.daily_quote(today)
    q2 = calendar_card.daily_quote(today)
    assert q1 and q1 == q2                       # 同一天稳定
    assert q1 in calendar_card.FALLBACK_QUOTES   # 来自内置语料


def test_quote_llm_result_cached(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.zhipu_api_key", "fake")
    monkeypatch.setattr(calendar_card, "_quote_llm", lambda t, c: "家有暖灯与热汤")
    assert calendar_card.daily_quote(dt.date(2026, 9, 1)) == "家有暖灯与热汤"
    # LLM 掉线后仍命中磁盘缓存（当天固定）
    monkeypatch.setattr(calendar_card, "_quote_llm", lambda t, c: None)
    assert calendar_card.daily_quote(dt.date(2026, 9, 1)) == "家有暖灯与热汤"


def test_quote_llm_rejects_overlong(monkeypatch):
    monkeypatch.setattr("app.config.settings.zhipu_api_key", "fake")
    from unittest.mock import patch
    fake = {"choices": [{"message": {"content": json.dumps({"quote": "超" * 40})}}]}
    with patch("app.calendar_card.requests.post") as p:
        p.return_value.json.return_value = fake
        p.return_value.raise_for_status = lambda: None
        q = calendar_card.daily_quote(dt.date(2027, 1, 1))
    assert q in calendar_card.FALLBACK_QUOTES   # 超长被拒 → 降级


# ---------- 内容源 / 节气节日事实 ----------

def test_source_registered_in_registry():
    from app.content_sources import resolve
    assert resolve("calendar-abc123") is calendar_card.SOURCE


def test_day_mark_priority_festival_over_jieqi():
    # 2026-10-01 国庆节（公历节日优先于节气）
    assert calendar_facts.day_mark(dt.date(2026, 10, 1)) == "国庆节"


def test_lunar_day_label_first_of_month():
    # 2026-08-13 是农历七月初一 → 显示月名「七月」
    assert calendar_facts.lunar_day_label(dt.date(2026, 8, 13)) == "七月"
    # 非初一显示日名
    assert calendar_facts.lunar_day_label(dt.date(2026, 8, 14)) == "初二"
