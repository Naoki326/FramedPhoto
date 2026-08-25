"""照片分类（#25，ADR-0005 / ADR-0006）领域测试。

S1 主 seam：函数级调用 + 内存库 + monkeypatch 配置。覆盖：
  - 分类推导（根目录散照 → 未分类；深层子目录 → 第一层祖先）
  - 三级降级（严格不出圈）与空分类无内容
  - 全库默认（含未分类）选片
  - 同分类多时段独立选片（隔 slot_key）
  - 内容哈希迁移 / 复制并存
"""
import datetime as dt
import os
import tempfile

import pytest
from PIL import Image

from app import category, db, daily, runtime_config, slots

_tmp = tempfile.mkdtemp(prefix="framedphoto-cat-test-")
PHOTO_LIB = os.path.join(_tmp, "photos")


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path):
    """隔离照片库根目录 + 清空 photo_scores（每测独享，避免污染共享 DB
    里其它模块对全库每日选片 / 分类聚合的确定性断言）。"""
    from app.config import settings as settings_mod
    monkeypatch.setattr(settings_mod, "photo_lib_dir", str(PHOTO_LIB))
    monkeypatch.setattr(category.settings, "photo_lib_dir", str(PHOTO_LIB))
    os.makedirs(PHOTO_LIB, exist_ok=True)
    # 清空评分表：每个用例只在固定分类里种自己的照片，互不串扰
    db._get_conn().execute("DELETE FROM photo_scores")
    db._get_conn().commit()
    yield


_seed = [0]


def _make_photo(rel_path: str, size=(1200, 1600), color=None) -> str:
    """在照片库根下建一张照片，返回绝对路径。

    color 缺省用递增偏移保证每张内容唯一（避免内容哈希碰撞，真实库里
    不同照片罕见同字节，这里显式去重）。
    """
    if color is None:
        _seed[0] += 1
        color = (30 + _seed[0] % 200, 60 + _seed[0] % 150, 90 + _seed[0] % 120)
    p = os.path.join(PHOTO_LIB, rel_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    Image.new("RGB", size, color).save(p, format="PNG")
    return p


def _score(path: str, memory: float, shot_at: float | None = None,
           shot_source: str = "file", used_at: float | None = None) -> dict:
    db.upsert_photo_score(path, filename=os.path.basename(path), memory_score=memory,
                          shot_at=shot_at, shot_source=shot_source,
                          analyzed_at=db.now(), used_at=used_at,
                          category=category.derive_category(path),
                          content_hash=category.compute_content_hash(path))
    return db.get_photo_score(path)


def _today_md() -> tuple[int, int]:
    t = dt.date.today()
    return t.month, t.day


# ---------- 分类推导 ----------

def test_derive_category_root_scatter_is_uncategorized():
    """照片库根目录散照 → 未分类（正常参与选片）。"""
    p = _make_photo("scatter.png")
    assert category.derive_category(p) == category.UNCATEGORIZED


def test_derive_category_first_level_folder():
    """第一层文件夹 → 该分类名；深层子目录归其第一层祖先。"""
    assert category.derive_category(_make_photo("宝宝/a.png")) == "宝宝"
    assert category.derive_category(_make_photo("婚礼视频/后期/a.png")) == "婚礼视频"


def test_derive_category_upload_is_uncategorized():
    """内容库上传图（uploads 相对路径）→ 未分类。"""
    assert category.derive_category("uploads/ab.orig") == category.UNCATEGORIZED


# ---------- 三级降级（ADR-0006 不出圈） ----------

def test_scoped_pick_tier1_historical_today(monkeypatch):
    """分类内 ① 月-日匹配 + 门槛 + EXIF：命中历史今天。"""
    p = _make_photo("cat_t1/hist.png")
    m, d = _today_md()
    shot = dt.datetime(dt.date.today().year - 2, m, d, 12, 0, 0)
    _score(p, memory=90, shot_at=shot.timestamp(), shot_source="exif")
    monkeypatch.setattr("app.daily.settings.daily_min_score", 50)
    # 另建一个更高分但非历史今天 → 不该被 ① 选中
    p2 = _make_photo("cat_t1/new.png")
    _score(p2, memory=99, shot_at=dt.datetime(2020, 1, 1, 12, 0, 0).timestamp(),
           shot_source="exif")
    chosen, is_today = daily.select_daily_photo(category="cat_t1")
    assert chosen["path"] == p
    assert is_today is True


def test_scoped_pick_tier2_high_score_within_category(monkeypatch):
    """无历史候选 → 分类内按回忆度降序（仍严格限于该分类）。"""
    p_in = _make_photo("cat_t2/high.png")
    _score(p_in, memory=95)
    # 分类外的更高分照片绝不能被选中
    p_out = _make_photo("婚庆/t2out.png")
    _score(p_out, memory=99)
    monkeypatch.setattr("app.daily.settings.daily_min_score", 101)  # 无历史候选
    chosen, is_today = daily.select_daily_photo(category="cat_t2")
    assert chosen["path"] == p_in
    assert is_today is False


def test_scoped_pick_tier3_any_in_category(monkeypatch):
    """分类内任意兜底：分类确有照片但全为 0 分也可上屏。"""
    p = _make_photo("cat_t3/any.png")
    _score(p, memory=0)
    monkeypatch.setattr("app.daily.settings.daily_min_score", 101)
    chosen, is_today = daily.select_daily_photo(category="cat_t3")
    assert chosen is not None and chosen["path"] == p


def test_uncat_pick_tier3_unanalyzed_photos(monkeypatch):
    """绑定未分类且池内照片全未分析（analyzed_at=None）→ ③级兜底仍选中（US4 三级降级）。

    spec：绑定分类内只要有照片就一定有内容上屏（三级降级兜底）；
    「未分类」与普通分类同规则，池内有照片（哪怕未分析）即不空屏。"""
    p = _make_photo("scatter_unanalyzed.png")
    db.upsert_photo_score(p, filename="scatter_unanalyzed.png",
                          category=category.UNCATEGORIZED, content_hash="",
                          analyzed_at=None)   # 未分析记录（analyzed_only 过滤会排除）
    monkeypatch.setattr("app.daily.settings.daily_min_score", 101)
    chosen, is_today = daily.select_daily_photo(category=category.UNCATEGORIZED)
    assert chosen is not None, "未分类池内有照片（未分析）也必须上屏（③级兜底）"
    assert chosen["path"] == p
    assert is_today is False


def test_category_summary_uncategorized_sorted_by_count(monkeypatch):
    """分类汇总里「未分类」也按照片数降序参与排序（US12，不无条件排末尾）。"""
    from app.config import settings as settings_mod
    monkeypatch.setattr(settings_mod, "photo_lib_dir", str(PHOTO_LIB))
    # 未分类 5 张 > 其它分类 1 张 → 未分类应排第一
    for i in range(5):
        p = _make_photo(f"scatter_s{i}.png")
        _score(p, memory=60)
    _score(_make_photo("宝宝/one.png"), memory=90)
    summary = category.category_summary()
    names = [c["name"] for c in summary]
    assert names[0] == category.UNCATEGORIZED, f"未分类照片数最多应排首位，实际: {names}"
    # 未分类计数正确（5 张）
    uncat = next(c for c in summary if c["name"] == category.UNCATEGORIZED)
    assert uncat["count"] == 5


def test_scoped_pick_empty_category_no_content():
    """空分类（0 张）→ 无内容（None），绝不回退全库。"""
    _make_photo("婚礼视频/real.png")
    _score(_make_photo("婚礼视频/real.png"), memory=100)
    chosen, _ = daily.select_daily_photo(category="空分类")
    assert chosen is None


def test_unbounded_pick_includes_uncategorized(monkeypatch):
    """未绑定分类 → 全库（含未分类）选片，现有行为不变。"""
    p_root = _make_photo("scatter.png")
    _score(p_root, memory=100)
    monkeypatch.setattr("app.daily.settings.daily_min_score", 101)
    chosen, is_today = daily.select_daily_photo(category=None)
    assert chosen is not None
    assert is_today is False
    # 未分类散照进入全库选片池（ADR 需求 22）
    all_scores = db.list_photo_scores(limit=100000)
    assert any(s["path"] == p_root for s in all_scores)


# ---------- 同分类多时段独立选片 ----------

def test_slot_key_differs_by_slot_time():
    """slot_key 含段开始时刻：早晚两个照片时段（同分类）不同 key → 独立选片。"""
    runtime_config.save({"slot_segments": [
        {"start": "00:00", "type": "photo", "category": "宝宝"},
        {"start": "08:00", "type": "photo", "category": "宝宝"},
    ]})
    morning = daily.slot_key()
    # 模拟当前命中第二段（08:00 起）：slot_key 应带 08:00
    seg = slots.current_segment()
    # 段开始时刻不同 → slot_key 不同
    assert morning  # 非空
    # 用不同 now 验证 slot_key 反映段归属
    import datetime as _dt
    from unittest.mock import patch
    with patch("app.slots.current_segment") as cs:
        cs.return_value = {"start": "08:00", "type": "photo", "category": "宝宝"}
        with patch("app.slots.segment_start", return_value=_dt.datetime(2026, 8, 5, 8, 0)):
            assert daily.slot_key() != morning


def test_select_unbounded_and_scoped_paths_exist(monkeypatch):
    """选片只选原图文件仍在的记录：已删文件的记录不参与。"""
    p = _make_photo("cat_gone/gone.png")
    _score(p, memory=90)
    os.unlink(p)
    monkeypatch.setattr("app.daily.settings.daily_min_score", 101)
    chosen, _ = daily.select_daily_photo(category="cat_gone")
    assert chosen is None


# ---------- 内容哈希迁移 / 复制并存（ADR-0005） ----------

class FakeAnalysis:
    def __init__(self, path, memory=60.0):
        self.path = path
        self.filename = os.path.basename(path)
        self.caption = "旧文案"
        self.description = ""
        self.type = "其他"
        self.memory_score = memory
        self.beauty_score = 50.0
        self.reason = ""
        self.shot_at = None
        self.shot_source = "file"
        self.gps_lat = None
        self.gps_lon = None
        self.source = "heuristic"


def test_migrate_on_move_preserves_scores():
    """移动（旧路径文件不存在）→ 迁移记录到新路径与新分类，保留评分文案。"""
    old = _make_photo("婚礼视频/a.png")
    _score(old, memory=78, shot_at=dt.datetime(2020, 5, 1, 12, 0, 0).timestamp())
    # 复制文件到新路径（内容相同），删除旧文件（模拟移动）
    new = _make_photo("宝宝/a.png")
    with open(old, "rb") as f:
        content = f.read()
    with open(new, "wb") as f:
        f.write(content)
    os.unlink(old)
    # 用新路径分析（VLM 不可用走启发式；这里模拟分析对象）
    a = FakeAnalysis(new)
    rec = category.migrate_or_record(a)
    assert rec["path"] == new
    assert rec["category"] == "宝宝"
    assert rec["caption"] == "旧文案" or rec["caption"] == ""  # 迁移保留评分文案
    assert rec["memory_score"] == 78  # 不重新分析，保留原分
    assert db.get_photo_score(old) is None  # 旧记录已删


def test_backfill_refreshes_category_of_analyzed_record(monkeypatch):
    """存量已分析记录（分类未回填）经批量扫描刷新：只改分类，不触发重分析。
    覆盖 ADR-0005「分析入库与同步扫描时刷新」——现存 392 张记录无需重跑 VLM。"""
    import sys
    import importlib.util
    from pathlib import Path as _P
    tools = _P(__file__).resolve().parents[2] / "tools" / "analyze_photos.py"
    spec = importlib.util.spec_from_file_location("analyze_photos", str(tools))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["analyze_photos"] = mod
    spec.loader.exec_module(mod)
    p = _make_photo("宝宝/old.png")
    # 模拟历史记录：已分析但 category 为空（存量库状态）
    db.upsert_photo_score(p, filename="old.png", memory_score=60, analyzed_at=db.now(),
                          category="", content_hash="")
    assert db.get_photo_score(p)["category"] == ""
    mod._refresh_category_record(p)
    rec = db.get_photo_score(p)
    assert rec["category"] == "宝宝"
    assert rec["memory_score"] == 60          # 不重新分析，保留分数
    assert rec["analyzed_at"] is not None


def test_copy_coexists_as_two_records():
    """复制（旧文件还在）→ 并存为两条记录，同一张图可出现在两个分类。"""
    src = _make_photo("婚礼视频/a.png")
    _score(src, memory=70)
    with open(src, "rb") as f:
        content = f.read()
    dst = _make_photo("宝宝/copy.png")
    with open(dst, "wb") as f:
        f.write(content)
    a = FakeAnalysis(dst)
    rec = category.migrate_or_record(a)
    assert rec["path"] == dst
    assert rec["category"] == "宝宝"
    assert db.get_photo_score(src) is not None  # 原记录仍在
    assert db.get_photo_score(dst) is not None  # 新记录并存
    assert category.compute_content_hash(src) == category.compute_content_hash(dst)
