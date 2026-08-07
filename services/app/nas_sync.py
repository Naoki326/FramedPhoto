"""NAS 单张照片上传工具。

pull（NAS -> 本地照片库）仍由 scripts/sync_nas.sh 负责；本模块只处理
内容库图片的单张 push，并在远端成功后更新本地 NAS 镜像，供管理台立即显示。
"""
from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from app import db
from app.config import settings

SERVICE_ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_EXT = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
_SCORE_FIELDS = {
    "filename", "caption", "description", "type", "memory_score", "beauty_score",
    "reason", "shot_at", "shot_source", "gps_lat", "gps_lon", "source",
    "analyzed_at", "used_at",
}


def _service_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else SERVICE_ROOT / path


def configured() -> bool:
    return bool(settings.nas_ssh_host and settings.nas_ssh_user and settings.nas_photo_dir)


def _host() -> str:
    host = settings.nas_ssh_host.strip()
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _target() -> str:
    return f"{settings.nas_ssh_user}@{_host()}"


def upload_dir() -> str:
    configured_dir = settings.nas_upload_dir.strip()
    if configured_dir:
        return configured_dir.rstrip("/")
    return f"{settings.nas_photo_dir.rstrip('/')}/FramedPhoto"


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


def _remote_mkdir(path: str) -> None:
    command = f"mkdir -p -- {shlex.quote(path)}"
    _run(_ssh_base() + [_target(), command], timeout=60)


def _remote_size(path: str) -> int:
    command = f"stat -c %s -- {shlex.quote(path)}"
    result = _run(_ssh_base() + [_target(), command], timeout=60)
    return int(result.stdout.strip())


def verify_remote_file(path: str, expected_size: int) -> bool:
    try:
        return _remote_size(path) == expected_size
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_name(image_id: str, filename: str) -> str:
    suffix = Path(filename or "").suffix
    if not _ALLOWED_EXT.fullmatch(suffix):
        suffix = ".jpg"
    return f"{image_id}{suffix.lower()}"


def _clone_photo_score(original: Path, mirror: Path, filename: str) -> None:
    """把上传时已有的评分复制到 NAS 镜像；没有评分则留下待分析记录。"""
    # photo_scores 的 key 可能是绝对路径、services 下相对路径（services/...）或
    # 上传目录相对路径（uploads/...），全试一遍避免因路径形式不一致克隆失败
    candidates = [str(original), str(original.resolve())]
    if original.is_relative_to(SERVICE_ROOT):
        candidates.append(str(original.relative_to(SERVICE_ROOT)))
    score = None
    for path in candidates:
        score = db.get_photo_score(path)
        if score:
            break
    mirror_key = str(mirror.relative_to(SERVICE_ROOT)) if mirror.is_relative_to(SERVICE_ROOT) else str(mirror)
    if score:
        fields = {key: score.get(key) for key in _SCORE_FIELDS if key in score}
        fields["filename"] = score.get("filename") or filename
        fields["source"] = "nas"
        db.upsert_photo_score(mirror_key, **fields)
    else:
        db.upsert_photo_score(
            mirror_key, filename=filename, source="nas", analyzed_at=None,
        )


def push_content_image(image_id: str, filename: str) -> dict:
    """上传一张内容库原图到 NAS，并写入本地 NAS 镜像。

    返回远端路径、本地镜像路径、大小和 SHA-256。任何一步失败都会抛异常，
    调用方只有在本函数成功后才能把状态标记为 saved。
    """
    if not configured():
        raise RuntimeError("未配置 NAS_SSH_HOST / NAS_SSH_USER / NAS_PHOTO_DIR")

    original = _service_path(settings.upload_dir) / f"{image_id}.orig"
    if not original.is_file():
        raise FileNotFoundError(f"内容库原图不存在: {original}")

    remote_name = _remote_name(image_id, filename)
    remote_dir = upload_dir()
    remote_path = f"{remote_dir}/{remote_name}"
    _remote_mkdir(remote_dir)

    args = [
        _rsync_bin(), "-rltz", "--partial", "--timeout=60",
        "--chmod=Du+rwx,Dg+rx,Fu+rw,Fgo+r",
        "-e", _ssh_transport(),
    ]
    if settings.nas_rsync_bwlimit > 0:
        args.append(f"--bwlimit={settings.nas_rsync_bwlimit}")
    if settings.nas_rsync_path:
        args.append(f"--rsync-path={settings.nas_rsync_path}")
    args.extend([str(original), f"{_target()}:{remote_path}"])
    _run(args)

    size = original.stat().st_size
    if _remote_size(remote_path) != size:
        raise RuntimeError("NAS 远端文件大小校验失败")
    sha256 = _sha256(original)

    mirror = _service_path(settings.photo_lib_dir) / "FramedPhoto" / remote_name
    mirror.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original, mirror)
    _clone_photo_score(original, mirror, filename)
    # 确保镜像在照片库立即可见：克隆后仍未标记已分析则补一个标记
    # （无评分时按 0 分出现在照片库，至少用户能看得到这张已推送到 NAS 的图）
    mirror_key = str(mirror.relative_to(SERVICE_ROOT)) if mirror.is_relative_to(SERVICE_ROOT) else str(mirror)
    score = db.get_photo_score(mirror_key)
    if score and not score.get("analyzed_at"):
        db.upsert_photo_score(mirror_key, analyzed_at=time.time())

    return {
        "remote_path": remote_path,
        "mirror_path": str(mirror.relative_to(SERVICE_ROOT)) if mirror.is_relative_to(SERVICE_ROOT) else str(mirror),
        "size": size,
        "sha256": sha256,
    }
