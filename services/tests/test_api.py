"""API 集成测试：上传 / 内容 / 设备 / OTA 全链路。

注意：必须在 import app 之前设置临时目录环境变量，
使 db / upload / ota 落到临时位置，避免污染开发数据。
"""
import io
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="framedphoto-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ["OTA_DIR"] = os.path.join(_tmp, "ota")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def _png_bytes(w=1200, h=1600, color=(128, 128, 128)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _esp32_fw_bytes() -> bytes:
    """构造一个带 ESP-IDF 魔数 0xE9 的假固件（仅用于测试）。"""
    return b"\xe9" + b"\x00" * 100 + b"test-firmware"


# ---------- 基础 ----------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "FramedPhoto" in r.text


# ---------- 图片 ----------

def test_upload_and_content():
    r = client.post("/api/images/upload", files={"file": ("a.png", _png_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["width"] == 1200 and meta["height"] == 1600
    img_id = meta["id"]

    # 内容清单
    r = client.get("/api/images/content")
    assert r.status_code == 200
    items = r.json()["images"]
    assert items and items[0]["id"] == img_id
    assert items[0]["url"].endswith("/raw")

    # raw 下载（FPS6 头）
    r = client.get(f"/api/images/{img_id}/raw")
    assert r.status_code == 200
    assert r.content[:4] == b"FPS6"
    assert len(r.content) == 20 + 1200 * 1600 // 2

    # preview
    r = client.get(f"/api/images/{img_id}/preview")
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_upload_invalid_file():
    r = client.post("/api/images/upload", files={"file": ("x.png", b"not an image", "image/png")})
    assert r.status_code == 422


def test_delete_image():
    r = client.post("/api/images/upload", files={"file": ("b.png", _png_bytes(10, 10), "image/png")})
    img_id = r.json()["id"]
    r = client.delete(f"/api/images/{img_id}")
    assert r.status_code == 200
    assert client.get(f"/api/images/{img_id}/raw").status_code == 404


def test_content_with_device_heartbeat():
    r = client.post("/api/images/upload", files={"file": ("c.png", _png_bytes(), "image/png")})
    img_id = r.json()["id"]
    r = client.get("/api/images/content?device_id=dev-test-1")
    assert r.status_code == 200
    # 设备心跳被附带记录
    devices = client.get("/api/devices").json()["devices"]
    assert any(d["id"] == "dev-test-1" and d["current_image"] == img_id for d in devices)


# ---------- 设备 ----------

def test_device_register_and_heartbeat():
    r = client.post("/api/devices/register", json={"device_id": "esp32-s3-001", "name": "客厅相框"})
    assert r.status_code == 200
    assert r.json()["registered"] is True

    r = client.post("/api/devices/esp32-s3-001/heartbeat", json={
        "firmware_version": "0.3.0",
        "battery": 87,
        "current_image": "abc123",
        "ip": "192.168.1.50",
    })
    assert r.status_code == 200

    dev = client.get("/api/devices/esp32-s3-001").json()
    assert dev["firmware_version"] == "0.3.0"
    assert dev["battery"] == 87
    assert dev["current_image"] == "abc123"


def test_device_heartbeat_auto_register():
    r = client.post("/api/devices/unknown-dev/heartbeat", json={"firmware_version": "0.3.0"})
    assert r.status_code == 200
    assert client.get("/api/devices/unknown-dev").status_code == 200


def test_device_list_empty():
    r = client.get("/api/devices")
    assert r.status_code == 200
    assert "devices" in r.json()


# ---------- OTA ----------

def test_ota_upload_and_manifest():
    r = client.post("/api/ota/upload",
                    files={"file": ("fw.bin", _esp32_fw_bytes(), "application/octet-stream")},
                    data={"version": "0.3.0"})
    assert r.status_code == 200, r.text
    fw = r.json()
    assert fw["version"] == "0.3.0"
    assert len(fw["sha256"]) == 64
    assert fw["size"] == len(_esp32_fw_bytes())

    # manifest：设备与最新一致 -> null
    r = client.get("/api/ota/manifest?current_version=0.3.0")
    assert r.json()["detail"] is None

    # 设备旧版本 -> 返回升级
    r = client.get("/api/ota/manifest?current_version=0.2.0")
    assert r.json()["detail"]["version"] == "0.3.0"

    # 下载
    r = client.get("/api/ota/download")
    assert r.status_code == 200
    assert r.content == _esp32_fw_bytes()


def test_ota_reject_non_firmware():
    r = client.post("/api/ota/upload", files={"file": ("x.bin", b"plain text", "application/octet-stream")})
    assert r.status_code == 422


# ---------- WiFi 配置下发 ----------

def test_wifi_config_set_and_deliver():
    client.post("/api/devices/register", json={"device_id": "wifi-dev-1"})

    # 设置 WiFi 配置 → pending
    r = client.put("/api/devices/wifi-dev-1/wifi-config", json={"ssid": "Home-5G", "password": "secret"})
    assert r.status_code == 200
    dev = client.get("/api/devices/wifi-dev-1").json()
    assert dev["wifi_ssid"] == "Home-5G"
    assert dev["wifi_pending"] == 1

    # 心跳 → 响应携带配置
    r = client.post("/api/devices/wifi-dev-1/heartbeat", json={"firmware_version": "0.1.0"})
    body = r.json()
    assert body["wifi_config"]["ssid"] == "Home-5G"
    assert body["wifi_config"]["password"] == "secret"

    # 设备应用后心跳（带确认）→ pending 清除，不再下发
    r = client.post("/api/devices/wifi-dev-1/heartbeat",
                    json={"wifi_config_applied": True})
    assert "wifi_config" not in r.json()
    assert client.get("/api/devices/wifi-dev-1").json()["wifi_pending"] == 0


def test_wifi_config_unknown_device_404():
    r = client.put("/api/devices/nope/wifi-config", json={"ssid": "x", "password": ""})
    assert r.status_code == 404


def test_wifi_config_reject_empty_ssid():
    client.post("/api/devices/register", json={"device_id": "wifi-dev-2"})
    r = client.put("/api/devices/wifi-dev-2/wifi-config", json={"ssid": "", "password": ""})
    assert r.status_code == 422


# ---------- SmartConfig 配网 ----------

def test_smartconfig_provision():
    r = client.post("/api/devices/smartconfig", json={"ssid": "Home-5G", "password": "secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True
    assert body["ssid"] == "Home-5G"


def test_smartconfig_reject_empty_ssid():
    r = client.post("/api/devices/smartconfig", json={"ssid": "", "password": ""})
    assert r.status_code == 422


def test_esptouch_sender():
    from app import esptouch
    r = esptouch.send_smartconfig("TestSSID", "pwd123", ip="192.168.1.50", duration_s=0.3)
    assert r["sent"] is True
    assert r["packets"] > 0
