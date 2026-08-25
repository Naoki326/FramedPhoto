"""nas_sync 单元测试：远端文件夹操作与照片库推回 NAS（ADR-0007）。

SSH/rsync 全部 monkeypatch，不触真实 NAS。
"""
from pathlib import Path

import pytest

from app import nas_sync


@pytest.fixture
def nas_config(monkeypatch):
    monkeypatch.setattr(nas_sync.settings, "nas_ssh_host", "ds923")
    monkeypatch.setattr(nas_sync.settings, "nas_ssh_user", "root")
    monkeypatch.setattr(nas_sync.settings, "nas_ssh_port", 22)
    monkeypatch.setattr(nas_sync.settings, "nas_photo_dir", "/volume1/photo")
    monkeypatch.setattr(nas_sync.settings, "nas_rsync_path", "")
    monkeypatch.setattr(nas_sync.settings, "nas_rsync_bwlimit", 0)


def test_configured_requires_all_fields(monkeypatch):
    monkeypatch.setattr(nas_sync.settings, "nas_ssh_host", "")
    assert not nas_sync.configured()


def test_remote_mkdir_builds_command(monkeypatch, nas_config):
    """remote_mkdir：SSH mkdir NAS_PHOTO_DIR/<name>；已存在 → RemoteExistsError。"""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return SimpleCompleted(0, "", "")

    monkeypatch.setattr(nas_sync.subprocess, "run", fake_run)
    path = nas_sync.remote_mkdir("宝宝")
    assert path == "/volume1/photo/宝宝"
    joined = " ".join(calls[-1])
    assert "mkdir" in joined and "/volume1/photo/宝宝" in joined

    def fake_exists(argv, **kw):
        return SimpleCompleted(1, "", "mkdir: cannot create directory: File exists")

    monkeypatch.setattr(nas_sync.subprocess, "run", fake_exists)
    with pytest.raises(nas_sync.RemoteExistsError):
        nas_sync.remote_mkdir("宝宝")


class SimpleCompleted:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_remote_rename_guards_target_exists(monkeypatch, nas_config):
    """remote_rename：目标已存在 → RemoteExistsError（防 mv 把源移进目标目录）。"""
    state = {"dirs": {"/volume1/photo/宝宝", "/volume1/photo/宝宝2024"}}

    def fake_run(argv, **kw):
        cmd = argv[-1]
        if "[ -e" in cmd:
            # 模拟 shell：new 已存在 → exit 3（探测分支）
            return SimpleCompleted(3, "EXISTS", "")
        return SimpleCompleted(0, "", "")

    monkeypatch.setattr(nas_sync.subprocess, "run", fake_run)
    with pytest.raises(nas_sync.RemoteExistsError):
        nas_sync.remote_rename("宝宝", "宝宝2024")


def test_remote_rename_ok(monkeypatch, nas_config):
    cmds = []

    def fake_run(argv, **kw):
        cmds.append(argv[-1])
        return SimpleCompleted(0, "", "")

    monkeypatch.setattr(nas_sync.subprocess, "run", fake_run)
    nas_sync.remote_rename("宝宝", "宝宝2024")
    assert any("mv" in c for c in cmds)


def test_remote_rmdir_maps_errors(monkeypatch, nas_config):
    """remote_rmdir：非空 → OSError；不存在 → FileNotFoundError。"""
    def run_with(out):
        def fake_run(argv, **kw):
            return SimpleCompleted(1, "", out)
        return fake_run

    monkeypatch.setattr(nas_sync.subprocess, "run",
                        run_with("rmdir: failed to remove: Directory not empty"))
    with pytest.raises(OSError):
        nas_sync.remote_rmdir("宝宝")

    monkeypatch.setattr(nas_sync.subprocess, "run",
                        run_with("rmdir: failed to remove: No such file or directory"))
    with pytest.raises(FileNotFoundError):
        nas_sync.remote_rmdir("宝宝")


def test_push_frame_dir_folder_and_root(monkeypatch, nas_config, tmp_path):
    """push_frame_dir：子文件夹 → NAS_PHOTO_DIR/<folder>；None → 只推根目录散照。"""
    lib = tmp_path / "photos"
    (lib / "宝宝").mkdir(parents=True)
    (lib / "宝宝" / "a.jpg").write_bytes(b"a")
    (lib / "root.jpg").write_bytes(b"r")
    monkeypatch.setattr(nas_sync.settings, "photo_lib_dir", str(lib))

    rsync_cmds, ssh_cmds = [], []

    def fake_run(argv, *, timeout=3600):
        joined = " ".join(argv)
        if joined.startswith("ssh"):
            ssh_cmds.append(joined)
            return SimpleCompleted(0, "", "")
        rsync_cmds.append(joined)
        assert "rsync" in argv[0]
        return SimpleCompleted(0, "", "")

    monkeypatch.setattr(nas_sync, "_run", fake_run)

    # 子文件夹：src = photos/宝宝/，目标 NAS_PHOTO_DIR/宝宝/
    r = nas_sync.push_frame_dir("宝宝")
    assert r["ok"] and r["remote_dir"] == "/volume1/photo/宝宝"
    assert rsync_cmds[-1].endswith("photos/宝宝/ root@ds923:/volume1/photo/宝宝/")

    # 根目录（None）：只推散照（--exclude=*/），不递归子文件夹
    r = nas_sync.push_frame_dir(None)
    assert r["ok"] and r["remote_dir"] == "/volume1/photo"
    assert "--exclude=*/" in rsync_cmds[-1]
    assert rsync_cmds[-1].endswith("photos/ root@ds923:/volume1/photo/")
