"""SQLite 数据层：设备、图片、固件元数据。

用标准库 sqlite3，模块级单连接 + 线程锁（uvicorn 单进程场景足够）。
连接懒初始化，便于测试时通过环境变量指向临时库。

连接自愈：每次访问前 stat 数据库文件，若文件已被替换/重写
（inode、mtime、size 任一变化），自动重建连接，避免长驻连接
读到陈旧数据（此前出现过的“上传后推送 404”即由此引起）。
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

from app.config import settings

_conn: sqlite3.Connection | None = None
_conn_sig: tuple[int, int, int] | None = None  # (st_ino, st_mtime_ns, st_size) 建连时的文件快照
_lock = threading.Lock()


def _db_path() -> Path:
    """解析数据库实际路径（相对路径按当前 cwd 归一化）。"""
    return Path(settings.db_path).resolve()


def _file_sig(path: Path) -> tuple[int, int, int] | None:
    """文件签名；文件不存在时返回 None。"""
    try:
        st = path.stat()
        return (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _init_schema(conn)
    return conn


def _get_conn() -> sqlite3.Connection:
    global _conn, _conn_sig
    if _conn is not None and _conn_sig == _file_sig(_db_path()):
        return _conn
    # 连接缺失，或数据库文件已被替换/重写 → 重建连接
    old = _conn
    _conn = None
    _conn_sig = None
    if old is not None:
        try:
            # 尝试把 WAL 落回主文件，避免重建后丢失未 checkpoint 的数据
            old.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass
        try:
            old.close()
        except sqlite3.Error:
            pass
    _conn = _open_conn()
    _conn_sig = _file_sig(_db_path())
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id              TEXT PRIMARY KEY,
            name            TEXT DEFAULT '',
            mac             TEXT DEFAULT '',
            firmware_version TEXT DEFAULT '',
            battery         REAL DEFAULT -1,
            current_image   TEXT DEFAULT '',
            ip              TEXT DEFAULT '',
            last_seen       REAL,
            created_at      REAL,
            wifi_ssid       TEXT DEFAULT '',
            wifi_password   TEXT DEFAULT '',
            wifi_pending    INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS images (
            id              TEXT PRIMARY KEY,
            filename        TEXT DEFAULT '',
            fps6_path       TEXT DEFAULT '',
            width           INTEGER,
            height          INTEGER,
            landscape       INTEGER DEFAULT 0,
            created_at      REAL,
            nas_status      TEXT DEFAULT '',
            nas_path        TEXT DEFAULT '',
            nas_sha256      TEXT DEFAULT '',
            nas_synced_at   REAL,
            nas_error       TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS firmware (
            version     TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            file_path   TEXT NOT NULL,
            sha256      TEXT NOT NULL,
            size        INTEGER NOT NULL,
            created_at  REAL
        );

        CREATE TABLE IF NOT EXISTS photo_scores (
            path            TEXT PRIMARY KEY,
            filename        TEXT DEFAULT '',
            caption         TEXT DEFAULT '',
            description     TEXT DEFAULT '',
            type            TEXT DEFAULT '',
            memory_score    REAL DEFAULT 0,
            beauty_score    REAL DEFAULT 0,
            reason          TEXT DEFAULT '',
            shot_at         REAL,
            shot_source     TEXT DEFAULT '',
            gps_lat         REAL,
            gps_lon         REAL,
            source          TEXT DEFAULT '',
            analyzed_at     REAL,
            used_at         REAL,
            category        TEXT DEFAULT '',
            content_hash    TEXT DEFAULT ''
        );
        """
    )
    conn.commit()
    # 存量库兼容：补新列
    for col, ddl in (("shot_source", "ALTER TABLE photo_scores ADD COLUMN shot_source TEXT DEFAULT ''"),
                     ("category", "ALTER TABLE photo_scores ADD COLUMN category TEXT DEFAULT ''"),
                     ("content_hash", "ALTER TABLE photo_scores ADD COLUMN content_hash TEXT DEFAULT ''"),
                     ("wifi_ssid", "ALTER TABLE devices ADD COLUMN wifi_ssid TEXT DEFAULT ''"),
                     ("wifi_password", "ALTER TABLE devices ADD COLUMN wifi_password TEXT DEFAULT ''"),
                     ("wifi_pending", "ALTER TABLE devices ADD COLUMN wifi_pending INTEGER DEFAULT 0"),
                     ("landscape", "ALTER TABLE images ADD COLUMN landscape INTEGER DEFAULT 0"),
                     ("nas_status", "ALTER TABLE images ADD COLUMN nas_status TEXT DEFAULT ''"),
                     ("nas_path", "ALTER TABLE images ADD COLUMN nas_path TEXT DEFAULT ''"),
                     ("nas_sha256", "ALTER TABLE images ADD COLUMN nas_sha256 TEXT DEFAULT ''"),
                     ("nas_synced_at", "ALTER TABLE images ADD COLUMN nas_synced_at REAL"),
                     ("nas_error", "ALTER TABLE images ADD COLUMN nas_error TEXT DEFAULT ''")):
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({ddl.split()[2]})")]
            if col not in cols:
                conn.execute(ddl)
                conn.commit()
        except sqlite3.OperationalError:
            pass


def now() -> float:
    return time.time()


# ---------- devices ----------

def upsert_device(dev_id: str, **fields) -> None:
    with _lock:
        conn = _get_conn()
        # INSERT OR IGNORE：已存在设备保留原行（REPLACE 会重置未更新列，如 wifi_ssid/name）
        conn.execute("INSERT OR IGNORE INTO devices (id, created_at, last_seen) VALUES (?, ?, ?)",
                     (dev_id, now(), now()))
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE devices SET {sets} WHERE id=?", (*fields.values(), dev_id))
        conn.commit()


def get_device(dev_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
    return dict(row) if row else None


def list_devices() -> list[dict]:
    with _lock:
        rows = _get_conn().execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
    return [dict(r) for r in rows]


# ---------- images ----------

def insert_image(img_id: str, filename: str, fps6_path: str, width: int, height: int,
                 landscape: int = 0) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO images (id, filename, fps6_path, width, height, landscape, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (img_id, filename, fps6_path, width, height, landscape, now()),
        )
        conn.commit()


def get_image(img_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM images WHERE id=?", (img_id,)).fetchone()
    return dict(row) if row else None


def update_image(img_id: str, **fields) -> None:
    if not fields:
        return
    with _lock:
        conn = _get_conn()
        sets = ", ".join(f"{key}=?" for key in fields)
        conn.execute(f"UPDATE images SET {sets} WHERE id=?", (*fields.values(), img_id))
        conn.commit()


def list_images() -> list[dict]:
    with _lock:
        rows = _get_conn().execute("SELECT * FROM images ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def delete_image(img_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM images WHERE id=?", (img_id,))
        conn.commit()
    return cur.rowcount > 0


# ---------- firmware ----------

def upsert_firmware(version: str, filename: str, file_path: str, sha256: str, size: int) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO firmware (version, filename, file_path, sha256, size, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (version, filename, file_path, sha256, size, now()),
        )
        conn.commit()


def get_latest_firmware() -> dict | None:
    with _lock:
        row = _get_conn().execute(
            "SELECT * FROM firmware ORDER BY created_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def get_firmware(version: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM firmware WHERE version=?", (version,)).fetchone()
    return dict(row) if row else None


# ---------- photo_scores（AI 分析结果） ----------

def upsert_photo_score(path: str, **fields) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("INSERT OR IGNORE INTO photo_scores (path) VALUES (?)", (path,))
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE photo_scores SET {sets} WHERE path=?", (*fields.values(), path))
        conn.commit()


def get_photo_score(path: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM photo_scores WHERE path=?", (path,)).fetchone()
    return dict(row) if row else None


def delete_photo_score(path: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM photo_scores WHERE path=?", (path,))
        conn.commit()


def list_photo_scores(limit: int = 200, analyzed_only: bool = True,
                      category: str | None = None) -> list[dict]:
    """照片评分列表；可选按分类过滤（category=None 不限制）。"""
    sql = "SELECT * FROM photo_scores"
    where = []
    if analyzed_only:
        where.append("analyzed_at IS NOT NULL")
    if category is not None:
        where.append("category = ?")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(memory_score, 0) DESC LIMIT ?"
    params = [category] if category is not None else []
    params.append(limit)
    with _lock:
        rows = _get_conn().execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def mark_photo_used(path: str, ts: float) -> None:
    upsert_photo_score(path, used_at=ts)


def list_categories() -> list[str]:
    """所有非空照片分类（按照片记录聚合；空分类由磁盘目录补足）。"""
    with _lock:
        rows = _get_conn().execute(
            "SELECT DISTINCT category FROM photo_scores WHERE category != ''").fetchall()
    return sorted(r[0] for r in rows)


def category_counts() -> dict[str, int]:
    """分类 → 已分析照片数（供 tab 排序与空分类合并显示；空串按未分类归并）。"""
    with _lock:
        rows = _get_conn().execute(
            "SELECT COALESCE(NULLIF(category, ''), '未分类') AS cat, COUNT(*) "
            "FROM photo_scores WHERE analyzed_at IS NOT NULL GROUP BY cat").fetchall()
    return {r[0]: r[1] for r in rows}


def photo_content_hash(path: str) -> str | None:
    """照片记录的内容哈希（迁移识别用）；无则 None。"""
    row = get_photo_score(path)
    if not row or not row.get("content_hash"):
        return None
    return row["content_hash"]


def find_photo_by_hash(content_hash: str) -> dict | None:
    """按内容哈希查记录（迁移识别）；命中返回记录，未命中返回 None。"""
    if not content_hash:
        return None
    with _lock:
        row = _get_conn().execute(
            "SELECT * FROM photo_scores WHERE content_hash = ?", (content_hash,)).fetchone()
    return dict(row) if row else None


def select_daily_candidates(target_md: tuple[int, int], min_score: float, limit: int = 20,
                            category: str | None = None) -> list[dict]:
    """历史上的今天：月-日 匹配所有年份的照片（须有真实 EXIF 拍摄时间）。

    category 非空时严格限于该分类（ADR-0006 不出圈）；None=全库。"""
    m, d = target_md
    sql = """
        SELECT * FROM photo_scores
        WHERE analyzed_at IS NOT NULL
          AND shot_at IS NOT NULL
          AND shot_source = 'exif'
          AND CAST(strftime('%m', shot_at, 'unixepoch') AS INTEGER) = ?
          AND CAST(strftime('%d', shot_at, 'unixepoch') AS INTEGER) = ?
          AND memory_score >= ?
    """
    params: list = [m, d, min_score]
    if category is not None:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY memory_score DESC LIMIT ?"
    params.append(limit)
    with _lock:
        rows = _get_conn().execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]
