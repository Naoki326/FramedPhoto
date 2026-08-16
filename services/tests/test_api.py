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

def test_upload_and_content(monkeypatch):
    """上传 → 内容库 / FPS6 下载 / 预览 / 照片时段内容清单。

    时段编排（ec3a10f）后 content 不再直接返回上传图：上传图自动
    入照片库，照片时段经每日精选上屏（内容指纹 id = daily-*）。
    固定照片时段，避免随运行时间变化。
    """
    from app import slots
    monkeypatch.setattr(slots, "current_slot", lambda now=None: slots.SLOT_PHOTO)

    r = client.post("/api/images/upload", files={"file": ("a.png", _png_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["width"] == 1200 and meta["height"] == 1600
    img_id = meta["id"]

    # 内容库列表：最新上传排第一
    r = client.get("/api/images")
    assert r.status_code == 200
    items = r.json()["images"]
    assert items and items[0]["id"] == img_id
    assert items[0]["raw_url"].endswith("/raw")

    # raw 下载（FPS6 头）
    r = client.get(f"/api/images/{img_id}/raw")
    assert r.status_code == 200
    assert r.content[:4] == b"FPS6"
    assert len(r.content) == 20 + 1200 * 1600 // 2

    # preview（五源统一：原图缩放 ~800px JPEG，T4 #14）
    r = client.get(f"/api/images/{img_id}/preview")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content[:3] == b"\xff\xd8\xff"

    # 内容清单（照片时段）：返回当日精选（内容指纹 id = daily-*）。
    # 具体选哪张由选片策略与会话内照片库状态决定，不在此过度断言。
    r = client.get("/api/images/content")
    assert r.status_code == 200
    data = r.json()
    assert data["source"] == "daily"
    first = data["images"][0]
    assert first["id"].startswith("daily-")
    # url/preview_url 统一为 /{id}/raw|preview 形态（T5 #15）
    assert first["url"] == f"/api/images/{first['id']}/raw"
    assert first["preview_url"] == f"/api/images/{first['id']}/preview"
    assert first["filename"]

    # 上传图已自动进入照片库评分表（可被选片/手动设为今日精选）
    scores = client.get("/api/analysis/scores?limit=500").json()["scores"]
    assert any(img_id in s["path"] for s in scores)


def test_upload_invalid_file():
    r = client.post("/api/images/upload", files={"file": ("x.png", b"not an image", "image/png")})
    assert r.status_code == 422


def test_delete_image():
    r = client.post("/api/images/upload", files={"file": ("b.png", _png_bytes(10, 10), "image/png")})
    img_id = r.json()["id"]
    r = client.delete(f"/api/images/{img_id}")
    assert r.status_code == 200
    assert client.get(f"/api/images/{img_id}/raw").status_code == 404


def test_daily_select_accepts_uploads_path():
    """回归：内容库上传图（uploads 路径）也能设为今日精选（此前 404）。"""
    from app import daily, runtime_config

    r = client.post("/api/images/upload",
                    files={"file": ("daily.png", _png_bytes(color=(20, 120, 220)), "image/png")})
    assert r.status_code == 200, r.text
    img_id = r.json()["id"]

    # 上传后异步分析已写入 photo_scores（TestClient 同步执行后台任务）
    scores = client.get("/api/analysis/scores?limit=500").json()["scores"]
    rec = next(s for s in scores if img_id in s["path"])
    uploads_path = rec["path"]  # 入库时的原始路径形式（上传图为 uploads/ 下）
    assert "/uploads/" in uploads_path.replace("\\", "/")

    # 用入库路径设置今日精选 → 此前 uploads 路径会被 404 拒绝
    r = client.post("/api/images/daily/select", json={"path": uploads_path})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # 配置已写入，且能被 daily_manual_pick 查到
    saved = runtime_config.load()["daily_manual"]
    assert saved["path"] == uploads_path
    pick = daily.daily_manual_pick()
    assert pick is not None and pick[0]["path"] == uploads_path

    # 越界路径仍被拒绝
    r = client.post("/api/images/daily/select", json={"path": "/etc/hosts"})
    assert r.status_code == 404

    # 取消手动指定，恢复自动
    r = client.delete("/api/images/daily/select")
    assert r.status_code == 200
    assert not runtime_config.load().get("daily_manual", {}).get("path")


def test_content_with_device_heartbeat(monkeypatch):
    """设备轮询 content 附带心跳：current_image 记录当前真正下发的内容 id。"""
    from app import slots
    monkeypatch.setattr(slots, "current_slot", lambda now=None: slots.SLOT_PHOTO)
    r = client.post("/api/images/upload", files={"file": ("c.png", _png_bytes(), "image/png")})
    assert r.status_code == 200
    r = client.get("/api/images/content?device_id=dev-test-1")
    assert r.status_code == 200
    served = r.json()["images"][0]["id"]   # 照片时段为 daily-* 指纹
    assert served
    devices = client.get("/api/devices").json()["devices"]
    assert any(d["id"] == "dev-test-1" and d["current_image"] == served for d in devices)


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
    r = esptouch.send_smartconfig("TestSSID", "pwd123", ip="192.168.1.50", duration_s=3.0)
    assert r["sent"] is True
    assert r["packets"] > 0


def test_esptouch_v2_sender():
    from app.esptouch import send_smartconfig_v2
    r = send_smartconfig_v2("TestSSID", "pwd123", b"FramedPhoto2024!",
                            ip="192.168.1.50", duration_s=2.5)
    assert r["sent"] is True
    assert r["packets"] > 0
    assert r["v2"] is True


def test_esptouch_v2_rejects_bad_key():
    from app.esptouch import send_smartconfig_v2
    try:
        send_smartconfig_v2("TestSSID", "pwd", b"short")
        assert False, "should raise"
    except ValueError:
        pass


# ---------- 天气卡片（T2 #3：落盘原图，预览改从原图） ----------

# 卡片原图用 6 色板之外的纯色（品红）：若预览/原图含该色，
# 内容必然来自原图而非 6 色帧还原（帧还原只能产出板内色）。
_UNIQUE_RGB = (255, 0, 255)


def _mock_weather(monkeypatch):
    """固定天气数据与卡片渲染（无网络 / 无字体 / 无 LLM），返回 weather_card 模块。"""
    from app import weather_card
    data = {
        "now": {"temp": "23", "text": "多云", "icon": "101", "feelsLike": "22",
                "humidity": "60", "windDir": "东北风", "windScale": "3", "city": "测试市"},
        "daily": [{"fxDate": "2026-08-16", "textDay": "多云", "tempMin": "20",
                   "tempMax": "28", "iconDay": "101"}],
    }
    monkeypatch.setattr(weather_card, "fetch_weather", lambda: data)
    monkeypatch.setattr(weather_card, "resolve_location", lambda: ("101010100", "测试市"))
    monkeypatch.setattr(weather_card, "_daily_design",
                        lambda style, today: dict(weather_card.DEFAULT_DESIGN))
    card = Image.new("RGB", (1600, 1200), _UNIQUE_RGB)
    monkeypatch.setattr(weather_card, "_render_card", lambda style, d, design: card)
    return weather_card


def test_weather_render_writes_same_stem_original(monkeypatch):
    """渲染成功 → 缓存目录出现与帧同 stem 的原图（横屏视角彩色 PNG）。"""
    wc = _mock_weather(monkeypatch)

    r = client.get("/api/images/weather/raw")
    assert r.status_code == 200, r.text
    assert r.content[:4] == b"FPS6"

    frames = list(wc.CACHE_DIR.glob("weather_*.fps6"))
    origs = list(wc.CACHE_DIR.glob("weather_*.png"))
    assert len(frames) == 1, frames
    assert len(origs) == 1, origs
    assert frames[0].stem == origs[0].stem
    img = Image.open(io.BytesIO(origs[0].read_bytes()))
    assert img.size == (1600, 1200)          # 横屏用户视角
    assert img.getpixel((800, 600)) == _UNIQUE_RGB   # 原始彩色，未量化


def test_weather_preview_from_original_not_frame(monkeypatch):
    """预览内容来自落盘原图：含 6 色板外的品红色，不可能是帧还原产物。"""
    _mock_weather(monkeypatch)

    r = client.get("/api/images/weather/preview")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/jpeg")
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    r_, g_, b_ = img.getpixel((img.width // 2, img.height // 2))
    assert r_ > 180 and b_ > 180 and g_ < 120, (r_, g_, b_)


def test_weather_preview_unavailable_503(monkeypatch):
    """天气数据不可用 → 预览 503，无帧还原兑底路径。"""
    from app import weather_card
    monkeypatch.setattr(weather_card, "fetch_weather", lambda: None)

    assert client.get("/api/images/weather/preview").status_code == 503
    assert client.get("/api/images/weather/raw").status_code == 503


def test_weather_cache_rolling_cleanup_removes_originals_too(monkeypatch, tmp_path):
    """帧缓存滚动清理时，同 stem 原图一并成对清理，无孤儿原图残留。"""
    import os
    import time
    wc = _mock_weather(monkeypatch)
    wc.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 预置 10 对旧缓存（帧 + 同 stem 原图），mtime 递减
    stale_stems = []
    base = time.time() - 10 * 86400
    for i in range(10):
        stem = f"weather_20000101_old{i:02d}"
        stale_stems.append(stem)
        t = base - i * 3600
        for suffix in (".fps6", ".png"):
            p = wc.CACHE_DIR / f"{stem}{suffix}"
            p.write_bytes(b"x")
            os.utime(p, (t, t))

    r = client.get("/api/images/weather/raw")   # 触发渲染 + 滚动清理
    assert r.status_code == 200, r.text

    frames = {p.stem for p in wc.CACHE_DIR.glob("weather_*.fps6")}
    origs = {p.stem for p in wc.CACHE_DIR.glob("weather_*.png")}
    assert len(frames) <= wc.CACHE_KEEP          # 旧帧被滚动清理
    # 最旧的 3 对：帧与原图一并删除
    for stem in stale_stems[-3:]:
        assert stem not in frames and stem not in origs, stem
    # 剩余缓存或对完整：无孤儿原图，也无孤儿帧
    assert origs == frames
    # 当日新渲染的一对仍在
    assert any(not s.startswith("weather_20000101") for s in frames)


def test_weather_settings_clear_removes_originals_too(monkeypatch):
    """保存天气设置清帧缓存时，同 stem 原图同批清理（不残留孤儿）。"""
    wc = _mock_weather(monkeypatch)
    assert client.get("/api/images/weather/raw").status_code == 200
    frames = list(wc.CACHE_DIR.glob("weather_*.fps6"))
    origs = list(wc.CACHE_DIR.glob("weather_*.png"))
    assert frames and origs

    r = client.put("/api/settings", json={"qweather_city": "新城市"})
    assert r.status_code == 200, r.text
    assert not list(wc.CACHE_DIR.glob("weather_*.fps6"))
    assert not list(wc.CACHE_DIR.glob("weather_*.png"))


# ---------- 自由模块（T3 #6：降级文字卡入库 + 预览删帧还原回退） ----------

def _mock_free_degrade(monkeypatch):
    """自由模块降级：LLM key 清空、文生图失败 → 纯文字卡片（无网络）。"""
    from app import free_module as fm
    from app.config import settings
    monkeypatch.setattr(settings, "zhipu_api_key", "")
    monkeypatch.setattr(fm, "_generate_illustration", lambda prompt: None)
    monkeypatch.setattr(fm, "_cache_fps6", None)   # 防跨测试内存缓存泄漏
    monkeypatch.setattr(fm, "_cache_ts", 0)
    monkeypatch.setattr(fm, "_cache_key", "")
    monkeypatch.setattr(fm, "_cache_lib_id", None)
    return fm


def test_free_degraded_text_card_saved_and_preview_200(monkeypatch):
    """降级生成纯文字卡后，内容库出现该卡片原图，预览 200。"""
    from pathlib import Path
    from app import db
    from app.config import settings
    _mock_free_degrade(monkeypatch)

    r = client.get("/api/images/free/raw?refresh=true")
    assert r.status_code == 200, r.text
    assert r.content[:4] == b"FPS6"

    # 内容库出现该卡片原图（降级路径同样归档）
    cards = [m for m in db.list_images() if m["filename"].startswith("自由模块·")]
    assert cards, "降级文字卡应入库"
    up = Path(settings.upload_dir)
    assert (up / f"{cards[0]['id']}.orig").exists()

    # 预览 200（来自入库原图缩略，非帧还原）
    r = client.get("/api/images/free/preview")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/jpeg")


def test_free_preview_404_without_original(monkeypatch, tmp_path):
    """无任何卡片原图 → 404，不再有帧还原兑底（帧可渲染也不用于预览）。"""
    from app import free_module as fm
    from app.config import settings
    from app.epd_image import prepare_image
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    # 渲染可用（返回合法帧）也不得用于预览：若帧还原分支仍在，这里会 200 PNG
    frame = prepare_image(_png_bytes(1728, 1296, (10, 200, 10)), dither=True).data
    monkeypatch.setattr(fm, "render_free_fps6", lambda **kw: frame)

    r = client.get("/api/images/free/preview")
    assert r.status_code == 404, r.text
    assert "original" in r.json()["detail"]


def test_free_preview_stays_own_card_after_new_upload(monkeypatch):
    """回归（T4 #14）：自由模块预览在内容库新增上传后仍显示自己的卡片。

    旧实现全上传目录按 mtime 猜「最新 .orig」：上传新照片后自由模块
    预览错换成那张照片。收紧后原图获取只认自己落库的卡片（记录当前
    卡片的库 id）。泛化 /{free-id}/preview 与固定 /free/preview 两条
    路径都锁住：预览中心像素仍是卡片的奶油色插画底（文字卡背景
    255/247/230），不是新上传的绿照片（20/180/60）。
    """
    _mock_free_degrade(monkeypatch)   # 降级文字卡（奶油底），同样入库
    _set_single_slot("free")

    # 生成当前卡片（refresh 绕开缓存，确保用本用例的降级 mock）
    r = client.get("/api/images/free/raw?refresh=1")
    assert r.status_code == 200, r.text
    r = client.get("/api/images/content")
    assert r.status_code == 200, r.text
    free_id = r.json()["images"][0]["id"]
    assert free_id.startswith("free-"), free_id

    # 内容库新增上传一张绿照片（uploads 下 mtime 全场最新的 .orig）
    r = client.post("/api/images/upload",
                    files={"file": ("new-green.png", _png_bytes(1200, 1600, (20, 180, 60)),
                                    "image/png")})
    assert r.status_code == 200, r.text

    def assert_own_card(url: str):
        resp = client.get(url)
        assert resp.status_code == 200, (url, resp.text)
        assert resp.headers["content-type"].startswith("image/jpeg"), url
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        r_, g_, b_ = img.getpixel((img.width // 2, img.height // 2))
        # 卡片自己的奶油色底，而非新上传的绿照片（r/b 高、非绿色主导）
        assert r_ > 200 and b_ > 200 and not (g_ > r_), (url, r_, g_, b_)

    assert_own_card("/api/images/free/preview")
    assert_own_card(f"/api/images/{free_id}/preview")


# ---------- 每日精选 / 内容库预览与缩略图（T4 #4：删帧还原回退） ----------

def _fake_daily_meta(path: str) -> dict:
    """构造 ensure_daily() 返回形状的原图元数据，指向给定路径。"""
    return {
        "id": "daily-test",
        "path": path,
        "filename": "photo.png",
        "caption": "",
        "date": "",
        "memory_score": None,
        "width": 1200,
        "height": 1600,
        "landscape": 0,
    }


def test_daily_preview_200_from_original(monkeypatch, tmp_path):
    """每日精选预览 200：内容来自原图（品红不在 6 色板，非帧还原）。"""
    from app import daily
    photo = tmp_path / "daily_orig.png"
    photo.write_bytes(_png_bytes(1200, 1600, _UNIQUE_RGB))
    monkeypatch.setattr(daily, "ensure_daily", lambda: _fake_daily_meta(str(photo)))

    r = client.get("/api/images/daily/preview")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/jpeg")
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    r_, g_, b_ = img.getpixel((img.width // 2, img.height // 2))
    assert r_ > 180 and b_ > 180 and g_ < 120, (r_, g_, b_)


def test_daily_preview_404_without_original(monkeypatch, tmp_path):
    """每日精选原图失效 → 404；当日帧文件仍在也不得帧还原（否则会 200 PNG）。"""
    from app import daily
    from app.epd_image import prepare_image
    daily.DAILY_DIR.mkdir(parents=True, exist_ok=True)
    daily.DAILY_FILE.write_bytes(prepare_image(_png_bytes()).data)  # 当日已渲染的帧
    monkeypatch.setattr(daily, "ensure_daily",
                        lambda: _fake_daily_meta(str(tmp_path / "gone.png")))

    r = client.get("/api/images/daily/preview")
    assert r.status_code == 404, r.text
    assert "original" in r.json()["detail"]


def test_library_preview_thumbnail_and_download_serves_original():
    """内容库预览变为 ~800px JPEG 缩略图（五源统一观感，T4 #14）。

    缩略图内容来自原图（品红不在 6 色板，非帧还原）；全尺寸原图由既有
    下载端点 /{id}/download 承接（原样字节 + 附件文件名）。
    """
    png = _png_bytes(1200, 1600, _UNIQUE_RGB)
    r = client.post("/api/images/upload", files={"file": ("uniq.png", png, "image/png")})
    assert r.status_code == 200, r.text
    img_id = r.json()["id"]

    r = client.get(f"/api/images/{img_id}/preview")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/jpeg")
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    assert max(img.size) <= 800
    r_, g_, b_ = img.getpixel((img.width // 2, img.height // 2))
    assert r_ > 180 and b_ > 180 and g_ < 120, (r_, g_, b_)

    # 全尺寸原图由既有下载端点承接（行为不受预览泛化影响）
    d = client.get(f"/api/images/{img_id}/download")
    assert d.status_code == 200, d.text
    assert d.content == png
    assert "attachment" in d.headers["content-disposition"]


def test_library_preview_404_without_original():
    """内容库缺原图 → 预览 404；帧仍在也不得帧还原（否则会 200 PNG）。"""
    from pathlib import Path
    from app.config import settings
    r = client.post("/api/images/upload",
                    files={"file": ("nofb.png", _png_bytes(800, 600), "image/png")})
    img_id = r.json()["id"]
    up = Path(settings.upload_dir)
    (up / f"{img_id}.orig").unlink()
    assert (up / f"{img_id}.fps6").exists()

    r = client.get(f"/api/images/{img_id}/preview")
    assert r.status_code == 404, r.text
    assert "original" in r.json()["detail"]


def test_library_thumb_200_jpeg_from_original():
    """内容库缩略图 200：来自原图缩放（品红色非帧还原），宽度缩到 ~400。"""
    r = client.post("/api/images/upload",
                    files={"file": ("t.png", _png_bytes(1600, 1200, _UNIQUE_RGB), "image/png")})
    img_id = r.json()["id"]

    r = client.get(f"/api/images/{img_id}/thumb")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/jpeg")
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    assert img.width == 400
    r_, g_, b_ = img.getpixel((img.width // 2, img.height // 2))
    assert r_ > 180 and b_ > 180 and g_ < 120, (r_, g_, b_)


def test_library_thumb_404_without_original():
    """内容库缺原图与缩略缓存 → 缩略图 404；帧仍在也不得帧还原。"""
    from pathlib import Path
    from app.config import settings
    r = client.post("/api/images/upload",
                    files={"file": ("nofb2.png", _png_bytes(800, 600), "image/png")})
    img_id = r.json()["id"]
    up = Path(settings.upload_dir)
    for suffix in ("orig", "thumb.jpg"):
        (up / f"{img_id}.{suffix}").unlink(missing_ok=True)
    assert (up / f"{img_id}.fps6").exists()

    r = client.get(f"/api/images/{img_id}/thumb")
    assert r.status_code == 404, r.text
    assert "original" in r.json()["detail"]


# ---------- 临时显示 / 直推与校准（T5 #7：直推校验收编 + 当前显示删回退） ----------

def _clear_display():
    """清理临时显示状态（DISPLAY_FILE/META/ORIG 在模块级 Path 上，跨测试持久）。"""
    client.delete("/api/images/display/current")


def _valid_frame(w: int = 1200, h: int = 1600) -> bytes:
    """构造头/长度自洽的 FPS6 帧（零填充数据区；直推路径只校验不渲染）。"""
    import struct as _struct
    return _struct.pack("<4sIIBB6x", b"FPS6", w, h, 1, 1) + bytes(w * h // 2)


def _post_frame(payload: bytes, filename: str = "frame.fps6"):
    return client.post("/api/images/display/raw-fps6",
                       files={"file": (filename, payload, "application/octet-stream")})


def test_display_raw_fps6_rejects_bad_magic():
    """直推 magic 错（长度/尺寸自洽）→ 400 "not a FPS6 file"。"""
    _clear_display()
    bad = b"XXXX" + _valid_frame()[4:]
    r = _post_frame(bad)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "not a FPS6 file"


def test_display_raw_fps6_rejects_short_header():
    """直推连 20 字节头都不全 → 400 "not a FPS6 file"（与既有语义一致）。"""
    r = _post_frame(b"FPS6" + b"\x00" * 10)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "not a FPS6 file"


def test_display_raw_fps6_rejects_truncated():
    """直推截断帧（magic/尺寸对、数据区不足）→ 400 "truncated FPS6 payload"。"""
    truncated = _valid_frame()[:-4096]
    r = _post_frame(truncated)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "truncated FPS6 payload"


def test_display_raw_fps6_rejects_bad_size():
    """直推尺寸不符（头写 800x600、长度自洽）→ 400 "size must be ..."。"""
    r = _post_frame(_valid_frame(800, 600))
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "size must be 1200x1600, got 800x600"


def test_display_raw_fps6_ok_then_preview_404():
    """直推合法帧成功；「当前显示」预览无原图 → 404（帧还原回退已删）。"""
    from app import display
    _clear_display()
    frame = _valid_frame()
    r = _post_frame(frame)
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["width"] == 1200 and meta["height"] == 1600
    assert meta["has_orig"] is False

    # 设备侧原样字节可下载；当前显示为 manual
    assert client.get("/api/images/display/raw").content == frame
    assert client.get("/api/images/display/current").json()["displaying"] == "manual"

    # 预览：直推帧无原图 → 404；若帧还原兜底仍在会 200 PNG
    r = client.get("/api/images/display/preview")
    assert r.status_code == 404, r.text
    assert "original" in r.json()["detail"]
    assert not display.DISPLAY_ORIG.exists()
    _clear_display()


def test_display_upload_preview_200_from_original():
    """普通临时上传（有原图）→ 预览 200，内容来自原图（品红非帧还原）。"""
    _clear_display()
    r = client.post("/api/images/display/upload",
                    files={"file": ("m.png", _png_bytes(1600, 1200, _UNIQUE_RGB), "image/png")})
    assert r.status_code == 200, r.text
    r = client.get("/api/images/display/preview")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/jpeg")
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    r_, g_, b_ = img.getpixel((img.width // 2, img.height // 2))
    assert r_ > 180 and b_ > 180 and g_ < 120, (r_, g_, b_)
    _clear_display()


def test_calibration_generate_validates_frame(monkeypatch):
    """校准图生成路径同样走统一帧校验：坏帧 400（中文消息保留）、不写 display；合法帧直推成功。"""
    from app import calibration as calib
    from app import display
    _clear_display()

    # magic 错
    monkeypatch.setattr(calib, "chart_fps6", lambda: b"XXXX" + b"\x00" * 96)
    r = client.post("/api/calibration/generate")
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "不是有效的 FPS6 文件"
    assert not display.DISPLAY_FILE.exists()

    # 截断
    monkeypatch.setattr(calib, "chart_fps6", lambda: _valid_frame()[:-4096])
    r = client.post("/api/calibration/generate")
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "FPS6 数据不完整"

    # 尺寸不符（长度自洽）
    monkeypatch.setattr(calib, "chart_fps6", lambda: _valid_frame(640, 480))
    r = client.post("/api/calibration/generate")
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "尺寸必须为 1200x1600，实际 640x480"

    # 合法帧 → 直推成功，display 收到原样字节
    good = _valid_frame()
    monkeypatch.setattr(calib, "chart_fps6", lambda: good)
    r = client.post("/api/calibration/generate")
    assert r.status_code == 200, r.text
    assert display.DISPLAY_FILE.read_bytes() == good
    _clear_display()


# ---------- 内容源注册表改道（T2 #12：三源经注册表分发，raw 字节与固定路由一致） ----------

def _set_single_slot(slot_type: str) -> None:
    """固定全天时段为单一类型（runtime_config 白名单路径，与 /content 同源）。"""
    from app import runtime_config
    runtime_config.save({"slot_segments": [{"start": "00:00", "type": slot_type}]})


# (源前缀, 对应固定 raw 路由；None = 无固定路由——内容库的设备下载端点
#  历来就是 /{id}/raw 本身，无独立固定路由可比)
_SOURCE_CASES = [
    ("daily", "/api/images/daily/raw"),
    ("weather", "/api/images/weather/raw"),
    ("free", "/api/images/free/raw"),
    ("display", "/api/images/display/raw"),
    ("library", None),
]


def _prepare_source(monkeypatch, prefix: str) -> str:
    """按既有流程（上传图片 / 置顶上传 / 桩外部依赖 / 时段配置）产出「当前有效 id」。

    五源（T3 #13）：display 经置顶显示上传（优先于一切时段内容）；
    library 经普通上传（id 即内容库查找键，无前缀）。
    """
    if prefix == "display":
        _clear_display()
        r = client.post("/api/images/display/upload",
                        files={"file": ("disp.png", _png_bytes(1600, 1200, (198, 40, 40)), "image/png")})
        assert r.status_code == 200, r.text
        return r.json()["id"]
    if prefix == "library":
        r = client.post("/api/images/upload",
                        files={"file": ("lib.png", _png_bytes(color=(40, 128, 60)), "image/png")})
        assert r.status_code == 200, r.text
        return r.json()["id"]
    if prefix == "daily":
        _set_single_slot("photo")
        r = client.post("/api/images/upload",
                        files={"file": ("contract.png", _png_bytes(color=(30, 90, 200)), "image/png")})
        assert r.status_code == 200, r.text
    elif prefix == "weather":
        _mock_weather(monkeypatch)
        _set_single_slot("weather")
    else:
        _mock_free_degrade(monkeypatch)
        _set_single_slot("free")
    r = client.get("/api/images/content")
    assert r.status_code == 200, r.text
    images = r.json()["images"]
    assert images, f"{prefix} 源应有当前内容"
    return images[0]["id"]


@pytest.mark.parametrize("prefix,fixed_raw_url", _SOURCE_CASES)
def test_raw_dispatch_matches_fixed_route_and_parses(monkeypatch, prefix, fixed_raw_url):
    """参数化契约测试（T2 #12 三源 → T3 #13 五源 → T4 #14 预览泛化）：每源跑三断言。

    1. 分发命中：内容指纹 id 经 /{id}/raw 返回 200（不误落其他源 404），
       有对应固定路由的四源字节与固定路由完全一致；内容库（无前缀 id
       落兜底源）与既有直查行为一致；
    2. raw 可过帧解析：parse_fps6 校验 magic / 长度公式通过；
    3. 预览泛化（T4 #14）：/{id}/preview 五源共用，统一原图缩放 ~800px JPEG。
    """
    from app.epd_image import parse_fps6
    content_id = _prepare_source(monkeypatch, prefix)

    if prefix != "library":
        assert content_id.startswith(prefix + "-"), content_id

    dispatched = client.get(f"/api/images/{content_id}/raw")
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.headers["content-type"] == "application/octet-stream"
    if fixed_raw_url is not None:
        fixed = client.get(fixed_raw_url)
        assert fixed.status_code == 200, fixed.text
        assert dispatched.content == fixed.content

    prepared = parse_fps6(dispatched.content)
    assert (prepared.width, prepared.height) == (1200, 1600)

    # 预览泛化（T4 #14）：同一 id 的预览统一为原图缩放 ~800px JPEG
    prev = client.get(f"/api/images/{content_id}/preview")
    assert prev.status_code == 200, prev.text
    assert prev.headers["content-type"].startswith("image/jpeg")
    pimg = Image.open(io.BytesIO(prev.content))
    assert max(pimg.size) <= 800

    if prefix == "display":
        # 置顶显示优先于时段，不清理会污染后续用例的 /content 断言
        _clear_display()


def test_raw_unknown_no_prefix_id_lands_library_404():
    """无前缀未知 id 落内容库兜底：404 文案与既有直查一致。"""
    r = client.get("/api/images/no-such-id-xyz/raw")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "image not found"


def test_raw_display_prefix_without_frame_404():
    """display- 前缀但当前无置顶显示 → 404（改道前后语义一致）。"""
    _clear_display()
    r = client.get("/api/images/display-deadbeef00/raw")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "no display image"


def test_news_prefix_alias_hits_free_source(monkeypatch):
    """news- 旧前缀命中自由模块（第二前缀）：同一时刻 news-* 与 free-* 的 raw 字节一致。"""
    _mock_free_degrade(monkeypatch)
    _set_single_slot("free")

    r = client.get("/api/images/content")
    assert r.status_code == 200, r.text
    free_id = r.json()["images"][0]["id"]
    assert free_id.startswith("free-"), free_id

    via_free = client.get(f"/api/images/{free_id}/raw")
    via_news = client.get(f"/api/images/news-{free_id[len('free-'):]}/raw")
    assert via_free.status_code == 200, via_free.text
    assert via_news.status_code == 200, via_news.text
    assert via_news.content == via_free.content
    assert via_news.content[:4] == b"FPS6"


def test_preview_raw_fps6_no_original_404():
    """泛化预览路由：直推帧（无原图）→ 404，不做帧还原兑底（T4 #14）。"""
    _clear_display()
    r = _post_frame(_valid_frame())
    assert r.status_code == 200, r.text
    content_id = r.json()["id"]

    p = client.get(f"/api/images/{content_id}/preview")
    assert p.status_code == 404, p.text
    assert "original" in p.json()["detail"]
    _clear_display()


def test_preview_generalized_from_original_not_frame():
    """泛化预览路由内容由原图而来：置顶上传品红图 → 预览含品红（非 6 色量化帧）。"""
    _clear_display()
    r = client.post("/api/images/display/upload",
                    files={"file": ("gen.png", _png_bytes(1600, 1200, _UNIQUE_RGB), "image/png")})
    assert r.status_code == 200, r.text
    content_id = r.json()["id"]

    p = client.get(f"/api/images/{content_id}/preview")
    assert p.status_code == 200, p.text
    assert p.headers["content-type"].startswith("image/jpeg")
    img = Image.open(io.BytesIO(p.content)).convert("RGB")
    r_, g_, b_ = img.getpixel((img.width // 2, img.height // 2))
    assert r_ > 180 and b_ > 180 and g_ < 120, (r_, g_, b_)
    _clear_display()


# ---------- 内容清单与当前显示改道（T5 #15：源 meta + url helper） ----------

def _manifest_for(monkeypatch, case: str) -> tuple[dict, str]:
    """按既有流程（置顶上传 / 桩外部依赖 / 时段配置 / 推送）产出某源的
    /content 清单条目与 source 取值。"""
    _clear_display()
    if case == "display":
        r = client.post("/api/images/display/upload",
                        files={"file": ("disp.png", _png_bytes(1600, 1200, (198, 40, 40)), "image/png")})
        assert r.status_code == 200, r.text
    elif case == "content_push":
        _set_single_slot("photo")
        r = client.post("/api/images/upload",
                        files={"file": ("pushed.png", _png_bytes(color=(90, 60, 200)), "image/png")})
        assert r.status_code == 200, r.text
        r = client.post("/api/images/content/push", json={"id": r.json()["id"]})
        assert r.status_code == 200, r.text
    elif case == "daily":
        _set_single_slot("photo")
        r = client.post("/api/images/upload",
                        files={"file": ("contract.png", _png_bytes(color=(30, 90, 200)), "image/png")})
        assert r.status_code == 200, r.text
    elif case == "weather":
        _mock_weather(monkeypatch)
        _set_single_slot("weather")
    else:
        _mock_free_degrade(monkeypatch)
        _set_single_slot("free")
    r = client.get("/api/images/content")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["images"], f"{case} 源应有当前内容"
    return body["images"][0], body["source"]


# 改道前逐字段的清单契约（对比基准：字段集与取值语义零变更）
_MANIFEST_FIELD_SETS = {
    "display": {"id", "filename", "landscape", "width", "height", "has_orig",
                "url", "preview_url"},
    "weather": {"id", "filename", "caption", "date", "memory_score",
                "width", "height", "landscape", "url", "preview_url"},
    "free": {"id", "filename", "caption", "date", "memory_score",
             "width", "height", "landscape", "url", "preview_url"},
    "content_push": {"id", "filename", "width", "height", "landscape",
                     "created_at", "url", "preview_url"},
    "daily": {"id", "filename", "caption", "date", "memory_score",
              "width", "height", "landscape", "url", "preview_url"},
}


@pytest.mark.parametrize("case", sorted(_MANIFEST_FIELD_SETS))
def test_content_manifest_fields_match_premigration_contract(monkeypatch, case):
    """对比测试（T5 #15 验收 1/2）：五源清单条目字段集与 source 取值
    与改道前逐字段一致；唯一差异是 url/preview_url 统一为 /{id}/raw|preview
    形态，自由模块 url 无查询参数。"""
    import re as _re
    item, source = _manifest_for(monkeypatch, case)

    assert source == case, (case, source)
    assert set(item) == _MANIFEST_FIELD_SETS[case], \
        f"{case}: 字段集与改道前不一致: {set(item) ^ _MANIFEST_FIELD_SETS[case]}"

    # url/preview_url 统一为 /{id}/raw|preview 形态（自由模块 url 无 ?module=）
    assert item["url"] == f"/api/images/{item['id']}/raw"
    assert item["preview_url"] == f"/api/images/{item['id']}/preview"
    assert "?" not in item["url"] and "?" not in item["preview_url"]

    if case != "content_push":   # 内容库推送 id 无前缀（查找键）
        assert item["id"].startswith(case + "-"), item["id"]

    if case == "weather":
        assert item["filename"] == "weather_card"
        assert item["caption"].startswith("天气 ·")
        assert item["memory_score"] is None
        assert (item["width"], item["height"]) == (1200, 1600)
        assert item["landscape"] == 1
        assert _re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", item["date"])
    elif case == "free":
        assert item["filename"] == "free_module"
        assert item["caption"]          # 模块名
        assert item["memory_score"] is None
        assert (item["width"], item["height"]) == (1200, 1600)
        assert item["landscape"] == 1
        assert _re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", item["date"])
    elif case == "display":
        assert item["filename"] == "disp.png"
        assert item["has_orig"] is True
        assert (item["width"], item["height"]) == (1200, 1600)
    elif case == "content_push":
        assert item["filename"] == "pushed.png"
        assert item["created_at"] is not None
    else:   # daily
        assert item["filename"]
        assert isinstance(item["caption"], str)

    if case == "display":
        _clear_display()


def test_display_current_item_urls_follow_id_form():
    """「当前显示」查询（T5 #15 第二处调用）：消费源 meta + url helper，
    item 字段集零变更，url/preview_url 统一为 /{id}/raw|preview 形态。"""
    _clear_display()
    r = client.post("/api/images/display/upload",
                    files={"file": ("cur.png", _png_bytes(1600, 1200, _UNIQUE_RGB), "image/png")})
    assert r.status_code == 200, r.text
    content_id = r.json()["id"]

    cur = client.get("/api/images/display/current")
    assert cur.status_code == 200, cur.text
    body = cur.json()
    assert body["displaying"] == "manual"
    item = body["item"]
    assert set(item) == {"id", "filename", "landscape", "width", "height",
                         "has_orig", "url", "preview_url"}
    assert item["id"] == content_id
    assert item["url"] == f"/api/images/{content_id}/raw"
    assert item["preview_url"] == f"/api/images/{content_id}/preview"

    # 新形态 url 实际可用：设备下载契约 + 预览由原图而来（泛化路由）
    assert client.get(item["url"]).headers["content-type"] == "application/octet-stream"
    assert client.get(item["preview_url"]).headers["content-type"].startswith("image/jpeg")
    _clear_display()


def test_device_flow_manifest_download_and_fingerprint_dedupe(monkeypatch):
    """设备流程端到端（T5 #15 验收 4）：轮询清单 → 按 url 下载成功；
    内容未变（指纹相同）不重复下载——id 语义仍是内容指纹，固件零改动。"""
    from app.epd_image import parse_fps6
    _mock_weather(monkeypatch)
    _set_single_slot("weather")

    last_id, downloaded = None, []
    for _ in range(3):   # 设备多轮轮询（当日卡片缓存内内容不变）
        r = client.get("/api/images/content")
        assert r.status_code == 200, r.text
        item = r.json()["images"][0]
        if item["id"] == last_id:
            continue   # 指纹相同：设备不重复下载（NVS 变更检测语义不变）
        resp = client.get(item["url"])
        assert resp.status_code == 200, (item["url"], resp.text)
        assert resp.headers["content-type"] == "application/octet-stream"
        prepared = parse_fps6(resp.content)
        assert (prepared.width, prepared.height) == (1200, 1600)
        downloaded.append(item["id"])
        last_id = item["id"]
    assert len(downloaded) == 1, f"内容未变不应重复下载: {downloaded}"
