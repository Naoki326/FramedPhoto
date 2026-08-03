"""SQLite 数据层：设备、图片、固件元数据。

用标准库 sqlite3，模块级单连接 + 线程锁（uvicorn 单进程场景足够）。
连接懒初始化，便于测试时通过环境变量指向临时库。
"""
import sqlite3
import threading
import time
from pathlib import Path

from app.config import settings

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(_conn)
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
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS images (
            id          TEXT PRIMARY KEY,
            filename    TEXT DEFAULT '',
            fps6_path   TEXT DEFAULT '',
            width       INTEGER,
            height      INTEGER,
            created_at  REAL
        );

        CREATE TABLE IF NOT EXISTS firmware (
            version     TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            file_path   TEXT NOT NULL,
            sha256      TEXT NOT NULL,
            size        INTEGER NOT NULL,
            created_at  REAL
        );
        """
    )
    conn.commit()


def now() -> float:
    return time.time()


# ---------- devices ----------

def upsert_device(dev_id: str, **fields) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("INSERT OR REPLACE INTO devices (id, created_at, last_seen) VALUES (?, ?, ?)",
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

def insert_image(img_id: str, filename: str, fps6_path: str, width: int, height: int) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO images (id, filename, fps6_path, width, height, created_at) VALUES (?,?,?,?,?,?)",
            (img_id, filename, fps6_path, width, height, now()),
        )
        conn.commit()


def get_image(img_id: str) -> dict | None:
    with _lock:
        row = _get_conn().execute("SELECT * FROM images WHERE id=?", (img_id,)).fetchone()
    return dict(row) if row else None


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
