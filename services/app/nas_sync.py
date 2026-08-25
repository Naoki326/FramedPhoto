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

import shlex
import shutil
import subprocess
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
