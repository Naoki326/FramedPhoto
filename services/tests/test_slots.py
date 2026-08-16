"""slots 时段编排测试：24h 时序分段（slot_segments）+ 旧三时段配置兼容。"""
import datetime as dt
from unittest.mock import patch

import pytest

from app import slots


def _t(hhmm: str) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    return dt.datetime(2026, 8, 5, h, m)


# ---------- 基础工具 ----------

def test_parse_range():
    assert slots.parse_range("00:00-10:00") == (0, 600)
    assert slots.parse_range("21:00-08:00") == (1260, 480)   # 跨天
    assert slots.parse_range("垃圾") is None


def test_to_minutes_hhmm():
    assert slots.to_minutes("00:00") == 0
    assert slots.to_minutes("23:59") == 1439
    assert slots.to_minutes("24:00") is None
    assert slots.to_minutes("abc") is None
    assert slots.to_hhmm(480) == "08:00"
    assert slots.to_hhmm(1500) == "01:00"   # 回绕


# ---------- slot_segments 规范路径 ----------

def test_canonicalize_basic():
    raw = [
        {"start": "00:00", "type": "weather"},
        {"start": "08:00", "type": "photo"},
        {"start": "19:00", "type": "free"},
    ]
    assert slots.canonicalize(raw) == [
        {"start": "00:00", "type": "weather", "module": None},
        {"start": "08:00", "type": "photo", "module": None},
        {"start": "19:00", "type": "free", "module": None},
    ]


def test_canonicalize_accepts_int_starts():
    # 兼容旧存储格式（分钟整数）
    assert slots.canonicalize([{"start": 480, "type": "photo"}]) == [
        {"start": "00:00", "type": "photo", "module": None},
    ]


def test_canonicalize_merges_adjacent_same_type():
    raw = [
        {"start": "06:00", "type": "weather"},
        {"start": "08:00", "type": "weather"},   # 与上相邻同类型 → 合并
        {"start": "19:00", "type": "free"},
    ]
    canon = slots.canonicalize(raw)
    starts = [slots.to_minutes(s["start"]) for s in canon]
    assert 6 * 60 in starts and 8 * 60 not in starts


def test_canonicalize_wraps_first_to_midnight():
    # 首段不在 00:00 → 按跨天在 00:00 切开（类型同最后一段）
    canon = slots.canonicalize([
        {"start": "08:00", "type": "photo"},
        {"start": "19:00", "type": "free"},
    ])
    assert canon[0] == {"start": "00:00", "type": "free", "module": None}
    assert canon[-1]["type"] == "free"
    for a, b in zip(canon, canon[1:]):
        assert slots.to_minutes(a["start"]) < slots.to_minutes(b["start"])


def test_canonicalize_repeated_type():
    # 早晚两个 weather 段（灵活分段的典型场景）
    canon = slots.canonicalize([
        {"start": "00:00", "type": "weather"},
        {"start": "08:00", "type": "photo"},
        {"start": "19:00", "type": "weather"},
    ])
    assert [s["start"] for s in canon] == ["00:00", "08:00", "19:00"]
    assert [s["type"] for s in canon] == ["weather", "photo", "weather"]


@pytest.mark.parametrize("bad", [
    [],
    [{"start": "25:00", "type": "photo"}],
    [{"start": "08:00", "type": "video"}],
    [{"start": "08:00"}],
    ["x", "y"],
    None,
])
def test_canonicalize_rejects_invalid(bad):
    with pytest.raises(ValueError):
        slots.canonicalize(bad)


# ---------- current_slot 命中 ----------

def _patch_segments(segs):
    return patch("app.slots.segments", return_value=segs)


def test_current_slot_matches_segments():
    segs = [
        {"start": "00:00", "type": "weather"},
        {"start": "08:00", "type": "photo"},
        {"start": "19:00", "type": "free"},
    ]
    with _patch_segments(segs):
        cases = [("00:00", "weather"), ("07:59", "weather"), ("08:00", "photo"),
                 ("18:59", "photo"), ("19:00", "free"), ("23:59", "free")]
        for t, expect in cases:
            assert slots.current_slot(_t(t)) == expect, t


def test_current_slot_empty_falls_back_photo():
    with _patch_segments([]):
        assert slots.current_slot(_t("12:00")) == slots.SLOT_PHOTO


# ---------- 旧三时段配置合成（兼容） ----------

def test_segments_synthesizes_legacy():
    fake = {
        "slot_weather": "00:00-08:00", "slot_weather_enabled": True,
        "slot_photo": "08:00-19:00",
        "slot_free": "19:00-24:00", "slot_free_enabled": True,
    }
    with patch("app.slots.runtime_config.get", return_value=None), \
         patch("app.slots.effective", lambda k, s, **kw: fake.get(k, getattr(s, k, None))):
        segs = slots.segments()
        assert segs == [
            {"start": "00:00", "type": "weather", "module": None},
            {"start": "08:00", "type": "photo", "module": None},
            {"start": "19:00", "type": "free", "module": None},
        ]


def test_segments_legacy_cross_day():
    fake = {
        "slot_weather_enabled": False,
        "slot_photo": "08:00-21:00",
        "slot_free": "21:00-08:00", "slot_free_enabled": True,
    }
    with patch("app.slots.runtime_config.get", return_value=None), \
         patch("app.slots.effective", lambda k, s, **kw: fake.get(k, getattr(s, k, None))):
        segs = slots.segments()
        assert segs[0] == {"start": "00:00", "type": "free", "module": None}   # 跨天段 00:00 起
        assert segs[-1] == {"start": "21:00", "type": "free", "module": None}
        for t, expect in [("06:00", "free"), ("08:00", "photo"), ("20:00", "photo"), ("21:00", "free")]:
            assert slots.current_slot(_t(t)) == expect, t


def test_segments_legacy_gap_filled_with_photo():
    fake = {
        "slot_weather_enabled": False,
        "slot_photo": "10:00-12:00",
        "slot_free_enabled": False,
        "slot_news_enabled": False,   # 旧键默认 True，显式关闭避免 free 误入
    }
    with patch("app.slots.runtime_config.get", return_value=None), \
         patch("app.slots.effective", lambda k, s, **kw: fake.get(k, getattr(s, k, None))):
        segs = slots.segments()
        assert segs[0]["type"] == slots.SLOT_PHOTO       # 00:00-10:00 间隙
        assert segs[-1]["type"] == slots.SLOT_PHOTO      # 12:00-24:00 间隙
        assert slots.current_slot(_t("06:00")) == slots.SLOT_PHOTO


def test_segments_prefers_slot_segments():
    stored = [{"start": "00:00", "type": "free"}, {"start": "12:00", "type": "photo"}]
    with patch("app.slots.runtime_config.get", return_value=stored):
        assert slots.segments() == [
            {"start": "00:00", "type": "free", "module": None},
            {"start": "12:00", "type": "photo", "module": None},
        ]


def test_segments_stored_int_format_ok():
    # 历史存过分钟整数的 runtime_config 也能正常读取
    stored = [{"start": 480, "type": "photo"}]
    with patch("app.slots.runtime_config.get", return_value=stored):
        assert slots.segments()[0]["start"] == "00:00"
        assert slots.current_slot(_t("12:00")) == slots.SLOT_PHOTO


# ---------- 旧键别名（B1 #19：别名解析住 runtime_config 内部） ----------

def _write_runtime_config(data: dict) -> None:
    """直写隔离后的 runtime_config.json（模拟历史文件；旧键无法经白名单保存）。"""
    import json
    from app import runtime_config
    runtime_config.CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_segments_synthesizes_from_legacy_news_keys():
    """现存含旧键的配置无需迁移：时段合成仍识别自由模块前身键
    （slot_news / slot_news_enabled → free 段），语义与消费点旧 or-链一致。"""
    _write_runtime_config({
        "slot_weather": "00:00-08:00", "slot_weather_enabled": True,
        "slot_photo": "08:00-19:00",
        "slot_news": "19:00-24:00", "slot_news_enabled": True,
    })
    assert slots.segments() == [
        {"start": "00:00", "type": "weather", "module": None},
        {"start": "08:00", "type": "photo", "module": None},
        {"start": "19:00", "type": "free", "module": None},
    ]


def test_segments_new_key_wins_over_legacy_news_key():
    """新旧键同时存在 → 新键优先（旧 or-链语义：新键有值时旧键不生效）。"""
    _write_runtime_config({
        "slot_weather_enabled": False,
        "slot_photo": "08:00-20:00",
        "slot_news": "20:00-22:00", "slot_news_enabled": True,
        "slot_free": "22:00-24:00", "slot_free_enabled": True,
    })
    segs = slots.segments()
    free_starts = [s["start"] for s in segs if s["type"] == "free"]
    assert free_starts == ["22:00"]           # 用新键 22:00 起的段，而非旧键 20:00
    assert slots.current_slot(_t("21:00")) == slots.SLOT_PHOTO   # 旧键时段已让位
    assert slots.current_slot(_t("22:30")) == slots.SLOT_FREE


def test_segments_legacy_news_disabled_drops_free():
    """旧键显式关闭（历史脏值字符串也按声明类型强转）→ 不合成 free 段。"""
    _write_runtime_config({
        "slot_weather_enabled": False,
        "slot_photo": "08:00-20:00",
        "slot_news": "20:00-24:00", "slot_news_enabled": "false",
    })
    segs = slots.segments()
    assert [s["type"] for s in segs if s["type"] != "photo"] == []   # 无 free 段，全日 photo


# ---------- segment_start（内容归属时刻，跨午夜追溯） ----------

def test_segment_start_basic():
    segs = [
        {"start": "00:00", "type": "free", "module": "历史故事"},
        {"start": "04:30", "type": "weather", "module": None},
        {"start": "22:02", "type": "free", "module": "历史故事"},
    ]
    with _patch_segments(segs):
        # 白天 weather 段：不匹配 free → None
        assert slots.segment_start(_t("10:00"), typ=slots.SLOT_FREE) is None
        # 普通 free 段（不跨 0 点）：段开始 = 当天该段 start
        assert slots.segment_start(_t("23:00"), typ=slots.SLOT_FREE) == \
            dt.datetime(2026, 8, 5, 22, 2)


def test_segment_start_cross_midnight():
    """凌晨前（22:02 历史故事）与凌晨后（00:00 历史故事）同 type+module →
    0 点后视为同一段延续，段开始追溯到前一天 22:02（0 点不换图）。"""
    segs = [
        {"start": "00:00", "type": "free", "module": "历史故事"},
        {"start": "04:30", "type": "weather", "module": None},
        {"start": "22:02", "type": "free", "module": "历史故事"},
    ]
    with _patch_segments(segs):
        assert slots.segment_start(_t("00:30"), typ=slots.SLOT_FREE) == \
            dt.datetime(2026, 8, 4, 22, 2)
        # 04:30 之后 weather 段 → None
        assert slots.segment_start(_t("06:00"), typ=slots.SLOT_FREE) is None


def test_segment_start_module_filter():
    """末段模块不同 → 不追溯；module 参数精确过滤。"""
    segs = [
        {"start": "00:00", "type": "free", "module": "历史故事"},
        {"start": "04:30", "type": "weather", "module": None},
        {"start": "22:02", "type": "free", "module": "童话故事"},
    ]
    with _patch_segments(segs):
        # 末段是「童话故事」≠ 首段「历史故事」→ 不追溯，段开始 = 当天 00:00
        assert slots.segment_start(_t("00:30"), typ=slots.SLOT_FREE) == \
            dt.datetime(2026, 8, 5, 0, 0)
        # 指定模块过滤
        assert slots.segment_start(_t("23:00"), typ=slots.SLOT_FREE,
                                   module="童话故事") == dt.datetime(2026, 8, 5, 22, 2)
        assert slots.segment_start(_t("23:00"), typ=slots.SLOT_FREE,
                                   module="历史故事") is None


def test_segment_start_single_full_day():
    # 单段全天 free（首段 == 末段）：0 点后是新的一天 → 段开始 = 当天 00:00
    segs = [{"start": "00:00", "type": "free", "module": None}]
    with _patch_segments(segs):
        assert slots.segment_start(_t("02:00"), typ=slots.SLOT_FREE) == \
            dt.datetime(2026, 8, 5, 0, 0)


def test_slot_label():
    assert slots.slot_label("weather") == "天气卡片"
    assert slots.slot_label("free") == "自由模块"
    assert slots.slot_label("photo") == "每日照片"


# ---------- 时段块固定模块（module 字段） ----------

def test_canonicalize_keeps_free_module():
    canon = slots.canonicalize([
        {"start": "00:00", "type": "free", "module": "宝宝背单词"},
        {"start": "12:00", "type": "photo"},
    ])
    assert canon[0]["module"] == "宝宝背单词"
    assert canon[1]["module"] is None      # 非 free 段忽略 module


def test_canonicalize_merges_only_same_module():
    # 相邻同类型但固定模块不同 → 不合并
    canon = slots.canonicalize([
        {"start": "00:00", "type": "free", "module": "读诗"},
        {"start": "08:00", "type": "free", "module": "读诗"},   # 同模块 → 合并
        {"start": "12:00", "type": "free", "module": "童话故事"},
    ])
    starts = [slots.to_minutes(s["start"]) for s in canon]
    assert starts == [0, 720]
    assert canon[0]["module"] == "读诗"
    assert canon[1]["module"] == "童话故事"


def test_canonicalize_wrap_keeps_module():
    # 跨天切开时 00:00 段继承最后一段的固定模块（单段覆盖全天）
    canon = slots.canonicalize([{"start": "08:00", "type": "free", "module": "今日新闻"}])
    assert canon == [{"start": "00:00", "type": "free", "module": "今日新闻"}]


def test_canonicalize_rejects_bad_module():
    for bad in [123, [], {"x": 1}, True]:
        with pytest.raises(ValueError):
            slots.canonicalize([{"start": "08:00", "type": "free", "module": bad}])


def test_current_segment_returns_module():
    segs = [
        {"start": "00:00", "type": "free", "module": "宝宝背单词"},
        {"start": "12:00", "type": "photo", "module": None},
    ]
    with _patch_segments(segs):
        assert slots.current_segment(_t("09:00"))["module"] == "宝宝背单词"
        assert slots.current_segment(_t("09:00"))["type"] == slots.SLOT_FREE
        assert slots.current_segment(_t("15:00"))["module"] is None
        assert slots.current_slot(_t("09:00")) == slots.SLOT_FREE
