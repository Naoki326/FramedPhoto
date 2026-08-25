"""NAS 同步功能测试：单图 push、远端校验、删除本地副本、手动同步 API。"""
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from app import db, nas_sync
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture(autouse=True)
def _nas_config(monkeypatch, tmp_path):
    """隔离 NAS 配置与镜像目录，避免写入真实 services/photos。"""
    monkeypatch.setattr(nas_sync.settings, "nas_ssh_host", "ds923")
    monkeypatch.setattr(nas_sync.settings, "nas_ssh_user", "root")
    monkeypatch.setattr(nas_sync.settings, "nas_ssh_port", 22)
    monkeypatch.setattr(nas_sync.settings, "nas_photo_dir", "/volume1/photo")
    monkeypatch.setattr(nas_sync.settings, "nas_upload_dir", "/volume1/photo/FramedPhoto")
    monkeypatch.setattr(nas_sync.settings, "nas_rsync_path", "")
    monkeypatch.setattr(nas_sync.settings, "nas_rsync_bwlimit", 0)
    mirror = tmp_path / "photos"
    mirror.mkdir()
    monkeypatch.setattr(nas_sync.settings, "photo_lib_dir", str(mirror))
    yield mirror


def _make_upload(tmp_path: Path) -> Path:
    """在隔离的 UPLOAD_DIR 里放一张假原图。"""
    upload_dir = Path(nas_sync.settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / "abc123.orig"
    path.write_bytes(b"fake-image-bytes")
    return path


def test_configured_requires_ssh():
    assert nas_sync.configured()


def test_push_content_image_success(monkeypatch, tmp_path):
    original = _make_upload(tmp_path)
    calls = []

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(nas_sync.subprocess, "run", fake_run)
    monkeypatch.setattr(nas_sync, "_remote_size", lambda path: original.stat().st_size)

    result = nas_sync.push_content_image("abc123", "测试照片.png")

    assert result["remote_path"] == "/volume1/photo/FramedPhoto/abc123.png"
    assert result["size"] == len(b"fake-image-bytes")
    assert result["sha256"]
    # 远端目录创建 + 远端大小校验各一次 ssh 调用，再加 rsync 一次
    assert any(cmd[0] == "ssh" for cmd in calls)
    assert any(cmd[0].endswith("rsync") for cmd in calls)
    # 本地 NAS 镜像已写入
    mirror = Path(nas_sync.settings.photo_lib_dir) / "FramedPhoto" / "abc123.png"
    assert mirror.read_bytes() == b"fake-image-bytes"
    # 评分记录克隆到镜像路径（测试目录为绝对路径 → 主键也是绝对路径）
    score = db.get_photo_score(str(mirror))
    assert score is not None and score.get("filename") == "测试照片.png"
    # US24：归档照片归入 FramedPhoto 分类（普通分类）——克隆评分时分类即修正，
    # 不等下次 analyze_photos 扫描（content_hash 相同走复制并存语义）
    assert score.get("category") == "FramedPhoto", \
        f"镜像应归 FramedPhoto 分类，实际: {score.get('category')!r}"


def test_push_remote_size_mismatch_raises(monkeypatch, tmp_path):
    _make_upload(tmp_path)
    monkeypatch.setattr(nas_sync.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(nas_sync, "_remote_size", lambda path: 0)
    with pytest.raises(RuntimeError, match="大小校验失败"):
        nas_sync.push_content_image("abc123", "x.jpg")


def test_verify_remote_file(monkeypatch):
    monkeypatch.setattr(nas_sync, "_remote_size", lambda path: 100)
    assert nas_sync.verify_remote_file("/x/y.jpg", 100)
    assert not nas_sync.verify_remote_file("/x/y.jpg", 99)
    monkeypatch.setattr(nas_sync, "_remote_size",
                        lambda path: (_ for _ in ()).throw(OSError("ssh down")))
    assert not nas_sync.verify_remote_file("/x/y.jpg", 100)


# ---------- API ----------

def _upload_image() -> str:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (120, 160), (10, 20, 30)).save(buf, format="PNG")
    r = client.post("/api/images/upload",
                    files={"file": ("nas-test.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_sync_nas_api_flow(monkeypatch, tmp_path):
    img_id = _upload_image()

    def fake_push(image_id, filename):
        original = Path(nas_sync.settings.upload_dir) / f"{image_id}.orig"
        mirror = Path(nas_sync.settings.photo_lib_dir) / "FramedPhoto" / f"{image_id}.png"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        mirror.write_bytes(original.read_bytes())
        return {"remote_path": f"/volume1/photo/FramedPhoto/{image_id}.png",
                "mirror_path": str(mirror), "size": original.stat().st_size,
                "sha256": "deadbeef"}

    monkeypatch.setattr(nas_sync, "push_content_image", fake_push)
    monkeypatch.setattr(nas_sync, "verify_remote_file", lambda path, size: True)

    # 开始同步 → uploading，且后台任务执行后变为 saved
    r = client.post(f"/api/images/{img_id}/sync-nas")
    assert r.status_code == 200
    assert r.json()["status"] == "uploading"
    meta = db.get_image(img_id)
    assert meta["nas_status"] == "saved"
    assert meta["nas_path"].endswith(f"{img_id}.png")

    # 已 saved 再点：幂等返回 saved
    r = client.post(f"/api/images/{img_id}/sync-nas")
    assert r.status_code == 200 and r.json()["status"] == "saved"

    # 删除本地副本：通过校验后删除文件与记录
    r = client.post(f"/api/images/{img_id}/delete-local")
    assert r.status_code == 200, r.text
    assert not (Path(nas_sync.settings.upload_dir) / f"{img_id}.orig").exists()
    assert db.get_image(img_id) is None


def test_delete_local_requires_saved(monkeypatch, tmp_path):
    img_id = _upload_image()
    monkeypatch.setattr(nas_sync, "verify_remote_file", lambda path, size: True)
    r = client.post(f"/api/images/{img_id}/delete-local")
    assert r.status_code == 409


def test_sync_nas_unconfigured(monkeypatch):
    img_id = _upload_image()
    monkeypatch.setattr(nas_sync.settings, "nas_ssh_host", "")
    r = client.post(f"/api/images/{img_id}/sync-nas")
    assert r.status_code == 503


def test_sync_start_api(monkeypatch):
    class FakePopen:
        pid = 4242
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    r = client.post("/api/sync/start")
    assert r.status_code == 200
    assert r.json()["status"] == "started"
