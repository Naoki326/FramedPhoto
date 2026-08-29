"""photo_relocate 单元测试：NAS 目录重组的镜像收敛（ADR-0007 pull 收敛）。

覆盖 sync_nas.sh 集成的两个阶段（纯函数，无 SSH / 无真实 NAS）：
- relocate_pre：rsync 前按 (basename, size, mtime) 把本地文件挪到远端新路径，
  让 rsync 只传真正新增的照片（NAS 重组不再全量重传）。
- relocate_post：rsync 后孤儿文件入回收站（NAS 已删、本地残留），
  photo_scores 失效路径按内容哈希迁移（ADR-0005 语义），无主记录删除。
"""
import hashlib
import sqlite3
import time
from pathlib import Path

from app import nas_sync

# 与 sync_nas_filters.sh 白名单一致的扩展名（抽样大小写形式）
IMG = ".jpg"


def _touch(p: Path, data: bytes, mtime: int) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    import os
    os.utime(p, (mtime, mtime))
    return p


def _manifest_entry(p: Path, photos: Path) -> tuple[int, int, str]:
    import os
    st = p.stat()
    return (st.st_size, int(st.st_mtime), p.relative_to(photos).as_posix())


# ---------- relocate_pre ----------

def test_pre_moves_renamed_to_new_path(tmp_path):
    """NAS 把 相片/x.jpg 挪到 婚前拍/x.jpg：本地按 (basename,size,mtime) 唯一匹配 → mv。"""
    photos = tmp_path / "photos"
    old = _touch(photos / "相片" / "x.jpg", b"image-data", mtime=1700000000)
    remote_new = photos / "婚前拍" / "x.jpg"
    manifest = [(old.stat().st_size, 1700000000, "婚前拍/x.jpg")]

    stats = nas_sync.relocate_pre(photos, manifest)

    assert not old.exists() and remote_new.is_file()
    assert stats["moved"] == 1
    # 挪空后旧目录应被清理
    assert not (photos / "相片").exists()


def test_pre_pairs_copies_without_collision(tmp_path):
    """复制场景：本地 2 份同内容、远端 2 个位置 → 一一配对，不撞目标。"""
    photos = tmp_path / "photos"
    a = _touch(photos / "相片" / "same.jpg", b"d", mtime=100)
    b = _touch(photos / "备份" / "same.jpg", b"d", mtime=100)
    manifest = [(1, 100, "宝宝/same.jpg"), (1, 100, "怀孕/same.jpg")]

    stats = nas_sync.relocate_pre(photos, manifest)

    assert stats["moved"] == 2
    assert (photos / "宝宝" / "same.jpg").is_file()
    assert (photos / "怀孕" / "same.jpg").is_file()
    assert not a.exists() and not b.exists()


def test_pre_leaves_unmatched_alone(tmp_path):
    """远端无 (basename,size,mtime) 匹配（内容被替换/版本变化）→ 不动，留给 post。"""
    photos = tmp_path / "photos"
    old = _touch(photos / "相片" / "x.jpg", b"old-version", mtime=1700000000)
    manifest = [(999, 1700000000, "婚前拍/x.jpg")]  # size 不同 → 不匹配

    stats = nas_sync.relocate_pre(photos, manifest)

    assert old.is_file() and stats["moved"] == 0


# ---------- relocate_post ----------

def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE photo_scores (path TEXT PRIMARY KEY, "
                 "filename TEXT, content_hash TEXT DEFAULT '')")
    return conn


def test_post_trashes_orphans_but_not_fresh_uploads(tmp_path):
    """孤儿（远端 basename+size 均无、mtime 早于本次同步）→ 回收站；
    刚落盘待推送 NAS 的上传（mtime 新于 cutoff）不动。"""
    photos = tmp_path / "photos"
    trash = tmp_path / "trash"
    orphan = _touch(photos / "相片" / "gone.jpg", b"gone", mtime=1000)
    fresh = _touch(photos / "上传" / "pending.jpg", b"pending", mtime=9999)
    manifest = [(5, 1000, "别的/other.jpg")]  # 无同名同大小条目
    db = _make_db(tmp_path / "t.db")
    db.commit()

    stats = nas_sync.relocate_post(photos, manifest, db_path=tmp_path / "t.db",
                                   trash_dir=trash, cutoff_ts=5000)

    assert not orphan.exists()
    assert (trash / "相片" / "gone.jpg").is_file()      # 进回收站，保留相对结构
    assert fresh.is_file()                               # 挂起的上传不动
    assert stats["trashed"] == 1


def test_post_keeps_copy_semantics(tmp_path):
    """远端仍有同 (basename,size)（复制未收敛）→ 保留，ADR-0005 显式复制行为。"""
    photos = tmp_path / "photos"
    dup = _touch(photos / "相片" / "same.jpg", b"d", mtime=100)
    manifest = [(1, 100, "宝宝/same.jpg")]  # 已在别处存在
    _make_db(tmp_path / "t.db").commit()

    stats = nas_sync.relocate_post(photos, manifest, db_path=tmp_path / "t.db",
                                   trash_dir=tmp_path / "trash", cutoff_ts=999999)

    assert dup.is_file() and stats["trashed"] == 0


def test_post_migrates_scores_by_hash(tmp_path):
    """photo_scores 失效路径按 content_hash（sha256 前 16 位）迁移，保留评分；
    hash 对不上的（照片被替换）记录删除。绝对与相对两种记录形式都支持。"""
    photos = tmp_path / "photos"
    x = _touch(photos / "婚前拍" / "x.jpg", b"v2", mtime=100)   # 绝对形式记录的照片
    w = _touch(photos / "宝宝" / "w.jpg", b"v3", mtime=100)    # 相对形式记录的照片
    orphan_v1 = _touch(photos / "相片" / "y.jpg", b"old-v1", mtime=100)
    manifest = [(2, 100, "婚前拍/x.jpg"), (2, 100, "宝宝/w.jpg"), (6, 100, "宝宝/z.jpg")]
    db = _make_db(tmp_path / "t.db")
    h = lambda b: hashlib.sha256(b).hexdigest()[:16]
    db.execute("INSERT INTO photo_scores VALUES (?,?,?)",
               (str(photos / "相片" / "x.jpg"), "x.jpg", h(b"v2")))   # 绝对形式（旧路径已失效）
    db.execute("INSERT INTO photo_scores VALUES (?,?,?)",
               ("photos/相片/w.jpg", "w.jpg", h(b"v3")))               # 相对形式（相对 services）
    db.execute("INSERT INTO photo_scores VALUES (?,?,?)",
               (str(orphan_v1), "y.jpg", h(b"old-v1")))                # 无处可迁 → 删除
    db.commit()

    stats = nas_sync.relocate_post(photos, manifest, db_path=tmp_path / "t.db",
                                   trash_dir=tmp_path / "trash", cutoff_ts=999999)

    rows = {r[0] for r in db.execute("SELECT path FROM photo_scores")}
    assert str(x) in rows, "绝对形式记录应迁移到新绝对路径"
    assert "photos/宝宝/w.jpg" in rows, "相对形式记录应保持相对形式"
    assert str(orphan_v1) not in rows, "无主记录应删除"
    assert stats["migrated"] == 2 and stats["dropped"] == 1


def test_read_manifest_pipe_separator(tmp_path):
    """清单行 size|mtime|relpath：群晖 busybox stat 不解释 \t，用 | 分隔；
    split("|", 2) 只切前两刀，路径含 | 也不切坏。"""
    m = tmp_path / "manifest.txt"
    m.write_text("12|1700000000|./宝宝/a.jpg\n7|1700000001|./杂|竖|b.jpg\n",
                 encoding="utf-8")
    out = nas_sync._read_manifest(str(m))
    assert out[0] == (12, 1700000000, "宝宝/a.jpg")  # ./ 前缀剥掉
    assert out[1][2] == "杂|竖|b.jpg"  # 含分隔符的路径完整保留


def test_post_expires_old_trash(tmp_path):
    """回收站里超过保留期的文件自动清理，保留期内的不动。"""
    trash = tmp_path / "trash"
    old = _touch(trash / "20250101" / "a.jpg", b"x", mtime=1000)
    recent = _touch(trash / "20250901" / "b.jpg", b"x", mtime=1756660000)
    photos = tmp_path / "photos"
    photos.mkdir()
    _make_db(tmp_path / "t.db").commit()

    stats = nas_sync.relocate_post(photos, [], db_path=tmp_path / "t.db",
                                   trash_dir=trash, cutoff_ts=999999,
                                   now_ts=1756700000, retain_seconds=7 * 86400)

    assert not old.exists() and recent.is_file()
    assert stats["trash_expired"] == 1
