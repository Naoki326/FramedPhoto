"""NAS 照片库同步与远端文件夹操作（ADR-0007）。

pull（NAS -> 本地照片库）由 scripts/sync_nas.sh 负责（launchd 定时 +
管理台手动触发）；本模块处理：
- push_frame_dir：照片库某文件夹（含根目录散照）增量推回 NAS——上传的
  对称操作，NAS /volume1/photo 是唯一共享照片目录与最终存储；
- 远端文件夹管理：mkdir / mv / rmdir over SSH，供管理台在 photo 里
  新建、重命名、删除空文件夹（本地镜像与评分迁移由 category 层负责）。

内容库单图归档（push_content_image）已随 NAS 归档功能退役，不恢复。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from app.config import settings

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _service_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else SERVICE_ROOT / path


def configured() -> bool:
    return bool(settings.nas_ssh_host and settings.nas_ssh_user and settings.nas_photo_dir)


def require_configured() -> None:
    if not configured():
        raise RuntimeError("NAS 未配置（NAS_SSH_HOST / NAS_SSH_USER / NAS_PHOTO_DIR）")


def _host() -> str:
    host = settings.nas_ssh_host.strip()
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _target() -> str:
    return f"{settings.nas_ssh_user}@{_host()}"


def nas_photo_dir() -> str:
    return settings.nas_photo_dir.rstrip("/")


def _ssh_base() -> list[str]:
    return [
        "ssh", "-p", str(settings.nas_ssh_port),
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=4",
    ]


def _ssh_transport() -> str:
    return " ".join(shlex.quote(x) for x in _ssh_base())


def _rsync_bin() -> str:
    for candidate in ("/opt/homebrew/bin/rsync", "/usr/local/bin/rsync"):
        if Path(candidate).is_file() and Path(candidate).stat().st_mode & 0o111:
            return candidate
    found = shutil.which("rsync")
    if not found:
        raise RuntimeError("找不到 rsync，请先安装 brew rsync")
    return found


def _run(cmd: list[str], *, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=True)


def _ssh(command: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """在 NAS 上执行一条 shell 命令；非零退出抛 CalledProcessError。"""
    return _run(_ssh_base() + [_target(), command], timeout=timeout)


# ---------- 远端文件夹操作（管理台新建 / 重命名 / 删除，ADR-0007） ----------

class RemoteExistsError(FileExistsError):
    """NAS 上目标文件夹已存在（新建 / 重命名撞名）。"""


def remote_mkdir(folder: str) -> str:
    """NAS photo 下新建文件夹；已存在 / 不可达抛异常，返回远端路径。"""
    require_configured()
    path = f"{nas_photo_dir()}/{folder}"
    proc = subprocess.run(_ssh_base() + [_target(), f"mkdir -- {shlex.quote(path)}"],
                          text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        if "exists" in (proc.stderr + proc.stdout).lower():
            raise RemoteExistsError(f"NAS 上文件夹已存在: {folder}")
        raise RuntimeError(f"NAS 创建文件夹失败: {(proc.stderr or proc.stdout).strip()}")
    return path


def remote_rename(old: str, new: str) -> None:
    """NAS photo 下重命名文件夹；目标已存在（exit 3）/ 源不存在抛异常。"""
    require_configured()
    base = nas_photo_dir()
    # 先探测目标存在再 mv（mv a b 在 b 为目录时会把 a 移进 b 里面，必须防）
    command = (f"old={shlex.quote(base + '/' + old)}; new={shlex.quote(base + '/' + new)}; "
               f"[ ! -e \"$old\" ] && echo NOSRC && exit 4; "
               f"[ -e \"$new\" ] && echo EXISTS && exit 3; "
               f"mv -- \"$old\" \"$new\"")
    proc = subprocess.run(_ssh_base() + [_target(), command],
                          text=True, capture_output=True, timeout=120)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 3 or "EXISTS" in out:
        raise RemoteExistsError(f"NAS 上目标文件夹已存在: {new}")
    if proc.returncode == 4 or "NOSRC" in out:
        raise FileNotFoundError(f"NAS 上文件夹不存在: {old}")
    if proc.returncode != 0:
        raise RuntimeError(f"NAS 重命名失败: {out}")


def remote_rmdir(folder: str) -> None:
    """NAS photo 下删除空文件夹（rmdir 只删空目录，非空自动失败）。"""
    require_configured()
    path = f"{nas_photo_dir()}/{folder}"
    proc = subprocess.run(_ssh_base() + [_target(), f"rmdir -- {shlex.quote(path)}"],
                          text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        out = (proc.stderr or proc.stdout).strip()
        if "not empty" in out.lower() or "Directory not empty" in out:
            raise OSError(f"NAS 上文件夹非空: {folder}")
        if "No such" in out:
            raise FileNotFoundError(f"NAS 上文件夹不存在: {folder}")
        raise RuntimeError(f"NAS 删除文件夹失败: {out}")


def remote_list_dirs() -> list[str]:
    """NAS photo 第一层文件夹名（本地镜像缺失/未同步时的兜底列表）。"""
    require_configured()
    result = _ssh(f"find {shlex.quote(nas_photo_dir())} -mindepth 1 -maxdepth 1 -type d "
                  f"-exec basename {{}} \\;")
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


# ---------- 照片库文件夹推回 NAS（上传的对称操作） ----------

def push_frame_dir(folder: str | None) -> dict:
    """把照片库某文件夹（folder=None 为根目录散照）增量推回 NAS。

    上传落本机照片库（镜像）后同步一份到 NAS_PHOTO_DIR/<folder>/，
    NAS 仍是照片的最终存储。增量 rsync：已推过的不重传。
    根目录（未分类）只推 photo 根下的散照文件，不动子文件夹。
    """
    if not configured():
        return {"ok": False, "reason": "NAS 未配置"}
    src = _service_path(settings.photo_lib_dir)
    if not src.is_dir():
        return {"ok": False, "reason": f"目录不存在: {src}"}
    args = [
        _rsync_bin(), "-rltz", "--partial", "--timeout=60",
        "--chmod=Du+rwx,Dg+rx,Fu+rw,Fgo+r",
        "-e", _ssh_transport(),
    ]
    if folder:
        src = src / folder
        if not src.is_dir():
            return {"ok": False, "reason": f"目录不存在: {src}"}
        remote_dir = f"{nas_photo_dir()}/{folder}"
        _ssh(f"mkdir -p -- {shlex.quote(remote_dir)}", timeout=60)
    else:
        # 根目录：只推散照（排除所有子文件夹），不递归触碰已同步的分类
        args.append("--exclude=*/")
        remote_dir = nas_photo_dir()
    if settings.nas_rsync_bwlimit > 0:
        args.append(f"--bwlimit={settings.nas_rsync_bwlimit}")
    if settings.nas_rsync_path:
        args.append(f"--rsync-path={settings.nas_rsync_path}")
    args.extend([f"{src}/", f"{_target()}:{remote_dir}/"])
    _run(args, timeout=3600)
    return {"ok": True, "folder": folder or "", "remote_dir": remote_dir}


# ---------- NAS 目录重组的镜像收敛（ADR-0007：pull 收敛） ----------
#
# NAS 侧手动整理（挪动/改名/删除）后，rsync 只认「路径+大小+mtime」，会把
# 挪过位置的照片当全新文件全量重传；本地旧路径文件与评分记录也无人收敛。
# 两个阶段把「移动」还原成 O(1) 操作（均由 sync_nas.sh 在 rsync 前后调用）：
# - relocate_pre：rsync 前，按 (basename, size, mtime) 把本地文件挪到远端
#   新路径，rsync 只传真正新增的照片（重组不再全量重传）；
# - relocate_post：rsync 后，NAS 已删除的本地孤儿入回收站（保护待推送的
#   新上传），photo_scores 失效路径按内容哈希迁移（ADR-0005 语义），
#   无主记录删除（照片被替换成另一版时旧评分无处安放）。

#: 与 sync_nas_filters.sh 白名单一致的图片扩展名
IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp",
                ".bmp", ".tif", ".tiff", ".cr2", ".nef", ".arw", ".dng",
                ".raf", ".orf", ".rw2", ".pef", ".srw", ".x3f"}
_SKIP_DIRS = {"@eaDir", "#recycle", "N8BookData"}


def _iter_images(photos_dir: Path):
    """遍历镜像目录下的图片文件（跳过 NAS 系统目录与隐藏目录）。"""
    for root, dirs, files in os.walk(photos_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if Path(f).suffix.lower() in IMG_SUFFIXES:
                yield Path(root) / f


def _cleanup_empty_dirs(base: Path) -> None:
    """自底向上删除空目录（不删 base 本身）。"""
    for root, dirs, files in os.walk(base, topdown=False):
        if not dirs and not files and Path(root) != base:
            try:
                Path(root).rmdir()
            except OSError:
                pass


def relocate_pre(photos_dir: Path, manifest: list[tuple[int, int, str]]) -> dict:
    """阶段一（rsync 前）：本地文件按 (basename, size, mtime) 挪到远端新路径。

    manifest 是远端清单 [(size, mtime, relpath), ...]（sync_nas.sh 经 SSH 拉取）。
    已就位（同路径同属性）的跳过；同键多份时按序一一配对（复制场景，
    ADR-0005：一份文件对一个远端位置）；无匹配的不动（留给阶段二判孤儿）。
    """
    photos_dir = Path(photos_dir)
    remote = {rel: (size, mtime) for size, mtime, rel in manifest}

    placed: set[str] = set()
    local_by_key: dict[tuple, list[Path]] = defaultdict(list)
    for p in _iter_images(photos_dir):
        st = p.stat()
        rel = p.relative_to(photos_dir).as_posix()
        if remote.get(rel) == (st.st_size, int(st.st_mtime)):
            placed.add(rel)
        else:
            local_by_key[(p.name, st.st_size, int(st.st_mtime))].append(p)

    remote_by_key: dict[tuple, list[str]] = defaultdict(list)
    for rel, attrs in remote.items():
        if rel not in placed:
            remote_by_key[(Path(rel).name, *attrs)].append(rel)

    moved = 0
    for key in sorted(local_by_key):
        slots = sorted(remote_by_key.get(key, []))
        for src, rel in zip(sorted(local_by_key[key]), slots):
            dst = photos_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():  # 保守：目标位置已有文件则不覆盖
                continue
            shutil.move(str(src), str(dst))
            moved += 1
    _cleanup_empty_dirs(photos_dir)
    return {"already": len(placed), "moved": moved,
            "left": sum(len(v) for v in local_by_key.values()) - moved}


def _content_hash(path: Path) -> str:
    """内容哈希，与 category.compute_content_hash 一致（sha256 前 16 位）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _resolve_record_path(record: str, photos_dir: Path) -> Path:
    """photo_scores.path 可能是绝对路径或相对路径（相对镜像目录的父目录，
    即服务 cwd=services/）。"""
    p = Path(record)
    return p if p.is_absolute() else photos_dir.parent / p


def relocate_post(photos_dir: Path, manifest: list[tuple[int, int, str]], *,
                  db_path: Path, trash_dir: Path, cutoff_ts: int,
                  now_ts: float | None = None,
                  retain_seconds: int = 7 * 86400) -> dict:
    """阶段二（rsync 后）：孤儿入回收站 + photo_scores 失效路径收敛。

    - 孤儿判定（三重保护后才入 trash_dir，保留相对结构，保留期内可找回）：
      1) 远端清单无此路径；2) 远端也无同 (basename, size)（复制语义保留，
      ADR-0005）；3) mtime 早于本次同步开始（刚落盘待推送 NAS 的上传不动）。
    - 评分迁移：path 失效的记录按内容哈希匹配本地新位置（保留评分文案），
      无处可迁的删除（照片已被替换/删除，旧评分成裂图僵尸）。
    - 回收站超过保留期的自动清理。
    """
    photos_dir, trash_dir = Path(photos_dir), Path(trash_dir)
    remote_paths = {rel for _, _, rel in manifest}
    remote_keys = {(Path(rel).name, size) for size, _, rel in manifest}

    trashed = 0
    for p in list(_iter_images(photos_dir)):
        st = p.stat()
        rel = p.relative_to(photos_dir).as_posix()
        if rel in remote_paths or (p.name, st.st_size) in remote_keys:
            continue
        if int(st.st_mtime) >= cutoff_ts:
            continue
        dst = trash_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dst))
        # 入站时刻作为回收站计时起点：孤儿照片 mtime 往往很老，
        # 不刷新的话刚入站就会被过期判定清掉，回收站失去意义
        now = time.time()
        os.utime(dst, (now, now))
        trashed += 1
    _cleanup_empty_dirs(photos_dir)

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        rows = conn.execute(
            "SELECT path, filename, content_hash FROM photo_scores").fetchall()
        valid = {r[0] for r in rows if _resolve_record_path(r[0], photos_dir).exists()}

        by_name: dict[str, list[Path]] = defaultdict(list)
        for p in _iter_images(photos_dir):
            by_name.setdefault(p.name, []).append(p)
        _hash_cache: dict[Path, str] = {}

        migrated = dropped = 0
        updates: list[tuple[str, str]] = []
        deletes: list[str] = []
        used: set[Path] = set()
        for path_rec, _, chash in rows:
            if path_rec in valid:
                continue
            hit = None
            if chash:
                for cand in by_name.get(Path(path_rec).name, []):
                    if cand in used:
                        continue
                    h = _hash_cache.get(cand)
                    if h is None:
                        h = _hash_cache[cand] = _content_hash(cand)
                    if h == chash:
                        hit = cand
                        break
            if hit is not None:
                used.add(hit)
                # 保持记录原有的绝对/相对形式
                new = hit if Path(path_rec).is_absolute() else hit.relative_to(
                    photos_dir.parent)
                updates.append((new.as_posix() if not Path(path_rec).is_absolute()
                                else str(hit), path_rec))
                migrated += 1
            else:
                deletes.append(path_rec)
                dropped += 1
        conn.execute("BEGIN")
        conn.executemany("UPDATE photo_scores SET path=? WHERE path=?", updates)
        conn.executemany("DELETE FROM photo_scores WHERE path=?",
                         [(d,) for d in deletes])
        conn.commit()
    finally:
        conn.close()

    # 回收站过期清理
    now = time.time() if now_ts is None else now_ts
    expired = 0
    if trash_dir.is_dir():
        for p in list(trash_dir.rglob("*")):
            if p.is_file() and p.stat().st_mtime < now - retain_seconds:
                p.unlink()
                expired += 1
        _cleanup_empty_dirs(trash_dir)

    return {"trashed": trashed, "trash_expired": expired,
            "migrated": migrated, "dropped": dropped}


def _read_manifest(path: str) -> list[tuple[int, int, str]]:
    """读远端清单文件：每行 size|mtime|relpath。

    分隔符用 |（群晖 busybox stat 不解释 \t）：split("|", 2) 只切前两刀，
    路径即使含 | 也完整落在第三段；size/mtime 是数字不会含分隔符。
    """
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        size, mtime, rel = line.split("|", 2)
        if rel.startswith("./"):
            rel = rel[2:]  # 远端 find . 输出带 ./ 前缀，与本地 relpath 对齐
        out.append((int(size), int(mtime), rel))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NAS 目录重组的镜像收敛工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_pre = sub.add_parser("relocate-pre", help="rsync 前：本地文件挪到远端新路径")
    p_pre.add_argument("--manifest", required=True)
    p_pre.add_argument("--photos", required=True)
    p_post = sub.add_parser("relocate-post", help="rsync 后：孤儿回收 + 评分收敛")
    p_post.add_argument("--manifest", required=True)
    p_post.add_argument("--photos", required=True)
    p_post.add_argument("--db", required=True)
    p_post.add_argument("--trash", required=True)
    p_post.add_argument("--cutoff", type=int, required=True,
                        help="本次同步开始时刻（unix 秒），新于此的不判孤儿")
    args = parser.parse_args(argv)

    manifest = _read_manifest(args.manifest)
    if args.cmd == "relocate-pre":
        stats = relocate_pre(Path(args.photos), manifest)
        print(f"[relocate-pre] 已就位 {stats['already']}，挪动 {stats['moved']}，"
              f"未匹配 {stats['left']}")
    else:
        stats = relocate_post(Path(args.photos), manifest, db_path=Path(args.db),
                              trash_dir=Path(args.trash), cutoff_ts=args.cutoff)
        print(f"[relocate-post] 孤儿入回收站 {stats['trashed']}（过期清理 "
              f"{stats['trash_expired']}），评分迁移 {stats['migrated']}，"
              f"删无主记录 {stats['dropped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
