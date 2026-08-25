"""API 集成测试：上传 / 内容 / 设备 / OTA 全链路。

注意：必须在 import app 之前设置临时目录环境变量，
使 db / upload / ota 落到临时位置，避免污染开发数据。
"""
import io
import json
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

def test_smartconfig_provision(monkeypatch):
    """配网接口响应契约：started + ssid 回显。

    真实发包走 esptouch 且要广播 30s（test_esptouch_sender 已单独覆盖
    短时真实发包），这里把后台任务替换为空实现，避免每次跑测试白等 30s。
    """
    calls = []

    def _noop_add_task(self, func, *args, **kwargs):
        calls.append(func.__name__)

    monkeypatch.setattr("starlette.background.BackgroundTasks.add_task", _noop_add_task)
    r = client.post("/api/devices/smartconfig", json={"ssid": "Home-5G", "password": "secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True
    assert body["ssid"] == "Home-5G"
    assert "_send_smartconfig" in calls


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


def _fresh_weather_cache():
    """清空天气卡片缓存（帧 + 同 stem 原图 + 当日设计）。

    渲染缓存为会话级共享隔离（conftest 公开设置 env，#20）：依赖
    「无当日缓存」或精确计数前置的用例，自行建立干净前置。
    """
    from app import weather_card
    for pattern in ("weather_*.fps6", "weather_*.png", "design_*.json"):
        for f in weather_card.CACHE_DIR.glob(pattern):
            f.unlink(missing_ok=True)


def test_weather_render_writes_same_stem_original(monkeypatch):
    """渲染成功 → 缓存目录出现与帧同 stem 的原图（横屏视角彩色 PNG）。

    渲染触发走泛化下载契约 /{id}/raw（固定 /weather/raw 已退役，T7 #17）。
    """
    wc = _mock_weather(monkeypatch)
    _fresh_weather_cache()
    _clear_display()
    _set_single_slot("weather")

    r = client.get("/api/images/content")
    assert r.status_code == 200, r.text
    content_id = r.json()["images"][0]["id"]
    assert content_id.startswith("weather-"), content_id

    r = client.get(f"/api/images/{content_id}/raw")
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
    _clear_display()
    _set_single_slot("weather")

    r = client.get("/api/images/content")
    assert r.status_code == 200, r.text
    content_id = r.json()["images"][0]["id"]

    r = client.get(f"/api/images/{content_id}/preview")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/jpeg")
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    r_, g_, b_ = img.getpixel((img.width // 2, img.height // 2))
    assert r_ > 180 and b_ > 180 and g_ < 120, (r_, g_, b_)


def test_weather_unavailable_404(monkeypatch):
    """天气数据不可用且无当日缓存 → 泛化路由 404，无帧还原兑底路径。

    固定路由时代的 503 语义随路由退役（T7 #17）；泛化 /{id}/raw 的
    不可用语义是 404（源 missing_detail，与各源改道语义一致），
    预览缺原图 → 404（承 ADR-0001）。
    """
    from app import weather_card
    monkeypatch.setattr(weather_card, "fetch_weather", lambda: None)
    _fresh_weather_cache()   # 前置：无当日缓存（会话共享缓存目录里可能有前序用例的当日卡）

    assert client.get("/api/images/weather-unavail00/raw").status_code == 404
    assert client.get("/api/images/weather-unavail00/preview").status_code == 404


def test_weather_cache_rolling_cleanup_removes_originals_too(monkeypatch, tmp_path):
    """帧缓存滚动清理时，同 stem 原图一并成对清理，无孤儿原图残留。"""
    import os
    import time
    wc = _mock_weather(monkeypatch)
    _set_single_slot("weather")   # 固定天气时段（避免依赖墙钟时刻）
    _fresh_weather_cache()   # 前置：只含本用例预置的旧缓存对
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

    r = client.get("/api/images/content")   # 触发渲染（清单元数据确保当前内容）
    assert r.status_code == 200, r.text
    r = client.get(f"/api/images/{r.json()['images'][0]['id']}/raw")   # 触发渲染 + 滚动清理
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
    _clear_display()
    _set_single_slot("weather")
    r = client.get("/api/images/content")
    assert r.status_code == 200, r.text
    assert client.get(f"/api/images/{r.json()['images'][0]['id']}/raw").status_code == 200
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
    """降级生成纯文字卡后，内容库出现该卡片原图，泛化预览 200。

    生成触发走 POST /free/regenerate（GET /free/raw 的 refresh 参数随固定
    路由退役，T7 #17），下载与预览走泛化 /{id}/raw|preview。
    """
    from pathlib import Path
    from app import db
    from app.config import settings
    _mock_free_degrade(monkeypatch)
    _set_single_slot("free")

    r = client.post("/api/images/free/regenerate")
    assert r.status_code == 200, r.text
    content_id = r.json()["item"]["id"]
    assert content_id.startswith("free-"), content_id

    dl = client.get(f"/api/images/{content_id}/raw")
    assert dl.status_code == 200, dl.text
    assert dl.content[:4] == b"FPS6"

    # 内容库出现该卡片原图（降级路径同样归档）
    cards = [m for m in db.list_images() if m["filename"].startswith("自由模块·")]
    assert cards, "降级文字卡应入库"
    up = Path(settings.upload_dir)
    assert (up / f"{cards[0]['id']}.orig").exists()

    # 预览 200（泛化路由，来自入库原图缩略，非帧还原）
    r = client.get(f"/api/images/{content_id}/preview")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/jpeg")


def test_free_preview_404_without_original(monkeypatch):
    """无任何卡片原图 → 泛化预览 404，不再有帧还原兑底（帧可渲染也不用于预览）。

    若帧还原分支仍在，这里会 200：render 可用但未入库（无库 id 可读原图）。
    """
    from app.epd_image import prepare_image
    fm = _mock_free_degrade(monkeypatch)
    frame = prepare_image(_png_bytes(1728, 1296, (10, 200, 10)), dither=True).data
    monkeypatch.setattr(fm, "render_free_fps6", lambda **kw: frame)

    r = client.get("/api/images/free-nooriginal00/preview")
    assert r.status_code == 404, r.text
    assert "original" in r.json()["detail"]


def test_free_preview_stays_own_card_after_new_upload(monkeypatch):
    """回归（T4 #14）：自由模块预览在内容库新增上传后仍显示自己的卡片。

    旧实现全上传目录按 mtime 猜「最新 .orig」：上传新照片后自由模块
    预览错换成那张照片。收紧后原图获取只认自己落库的卡片（记录当前
    卡片的库 id）。泛化 /{free-id}/preview 锁住：预览中心像素仍是
    卡片的奶油色插画底（文字卡背景 255/247/230），不是新上传的
    绿照片（20/180/60）。固定 /free/preview 随路由退役（T7 #17）。
    """
    _mock_free_degrade(monkeypatch)   # 降级文字卡（奶油底），同样入库
    _set_single_slot("free")

    # 生成本用例的降级 mock 卡片（重生成端点，不再借固定 raw 的 refresh）
    r = client.post("/api/images/free/regenerate")
    assert r.status_code == 200, r.text
    free_id = r.json()["item"]["id"]
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

    assert_own_card(f"/api/images/{free_id}/preview")


def test_free_regenerate_endpoint(monkeypatch):
    """POST /free/regenerate（T6 #16）：跳过当日内缓存强制重生成，走
    render_free_fps6(refresh=True) 同一条生成路径（选模块与 /content 一致）。

    已有当日内缓存时 POST 仍强制重生成（换一份 LLM 内容 → 内容指纹变化）；
    返回新卡片的清单条目，url/preview_url 为 /api/images/{id}/raw|preview
    形态且 url 实际可下载为合法 FPS6；内容清单与新条目一致。
    """
    fm = _mock_free_degrade(monkeypatch)
    _set_single_slot("free")

    # 先经 /content 生成当日卡片（建立当日内缓存）
    r = client.get("/api/images/content")
    assert r.status_code == 200, r.text
    id_before = r.json()["images"][0]["id"]
    assert id_before.startswith("free-"), id_before

    # 换一份 LLM 内容后 POST 重生成：跳过缓存、走同一生成路径 → 新指纹
    monkeypatch.setattr(fm, "_llm_content",
                        lambda module, today: {"title": "重生成测试卡", "body": "第二张卡片",
                                               "image_prompt": ""})
    r = client.post("/api/images/free/regenerate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    item = body["item"]
    assert item["id"].startswith("free-"), item
    assert item["url"] == f"/api/images/{item['id']}/raw"
    assert item["preview_url"] == f"/api/images/{item['id']}/preview"

    dl = client.get(item["url"])
    assert dl.status_code == 200, dl.text
    assert dl.headers["content-type"] == "application/octet-stream"
    assert dl.content[:4] == b"FPS6"

    # 清单条目与重生成结果一致，且确实重新生成（指纹变化）
    r = client.get("/api/images/content")
    assert r.json()["images"][0]["id"] == item["id"]
    assert item["id"] != id_before


def test_free_regenerate_503_when_free_disabled():
    """自由模块停用时 POST /free/regenerate → 503，与旧 refresh 参数失败语义一致。"""
    r = client.put("/api/settings", json={"free_enabled": False})
    assert r.status_code == 200, r.text
    r = client.post("/api/images/free/regenerate")
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "free module unavailable"


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
    """每日精选预览 200：内容来自原图（品红不在 6 色板，非帧还原）。

    走泛化 /{id}/preview（固定 /daily/preview 已退役，T7 #17）；
    id 不是查找键，任意 daily- 前缀 id 都服务当前精选。
    """
    from app import daily
    photo = tmp_path / "daily_orig.png"
    photo.write_bytes(_png_bytes(1200, 1600, _UNIQUE_RGB))
    monkeypatch.setattr(daily, "ensure_daily", lambda: _fake_daily_meta(str(photo)))

    r = client.get("/api/images/daily-anycard0000/preview")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/jpeg")
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    r_, g_, b_ = img.getpixel((img.width // 2, img.height // 2))
    assert r_ > 180 and b_ > 180 and g_ < 120, (r_, g_, b_)


def test_daily_preview_404_without_original(monkeypatch, tmp_path):
    """每日精选原图失效 → 泛化预览 404；当日帧文件仍在也不得帧还原
    （否则会 200）。"""
    from app import daily
    from app.epd_image import prepare_image
    daily.DAILY_DIR.mkdir(parents=True, exist_ok=True)
    daily.DAILY_FILE.write_bytes(prepare_image(_png_bytes()).data)  # 当日已渲染的帧
    monkeypatch.setattr(daily, "ensure_daily",
                        lambda: _fake_daily_meta(str(tmp_path / "gone.png")))

    r = client.get("/api/images/daily-nocard00000/preview")
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
    content_id = meta["id"]

    # 设备侧原样字节可下载（泛化下载契约）；当前显示为 manual
    assert client.get(f"/api/images/{content_id}/raw").content == frame
    assert client.get("/api/images/display/current").json()["displaying"] == "manual"

    # 预览：直推帧无原图 → 404；若帧还原兜底仍在会 200 PNG
    r = client.get(f"/api/images/{content_id}/preview")
    assert r.status_code == 404, r.text
    assert "original" in r.json()["detail"]
    assert not display.DISPLAY_ORIG.exists()
    _clear_display()


def test_display_upload_preview_200_from_original():
    """普通临时上传（有原图）→ 泛化预览 200，内容来自原图（品红非帧还原）。"""
    _clear_display()
    r = client.post("/api/images/display/upload",
                    files={"file": ("m.png", _png_bytes(1600, 1200, _UNIQUE_RGB), "image/png")})
    assert r.status_code == 200, r.text
    content_id = r.json()["id"]
    r = client.get(f"/api/images/{content_id}/preview")
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


# ---------- 设置回显：旧键别名与类型化强转（B1 #19） ----------

def _write_runtime_config(data: dict, monkeypatch) -> None:
    """直写隔离后的 runtime_config.json（模拟老版本管理台保存的现存文件）。

    走 PUT 无法写入旧键（白名单只收新键），旧键只能来自历史文件。
    """
    from app import runtime_config
    runtime_config.CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_settings_config(monkeypatch) -> dict:
    """GET /api/settings（隔离 IP 定位网络调用）→ config 段。"""
    from app import weather_card
    monkeypatch.setattr(weather_card, "ip_location", lambda: {"city": "测试市"})
    r = client.get("/api/settings")
    assert r.status_code == 200, r.text
    return r.json()["config"]


def test_settings_echo_resolves_legacy_keys(monkeypatch):
    """现存含旧键的 runtime_config.json（自由模块前身「新闻卡片」时代保存）
    无需迁移：设置回显直接给出新键语义（slot_news → slot_free），
    开关为真布尔（历史脏值 "true" 字符串也按注册处声明类型强转）。"""
    _write_runtime_config({
        "slot_weather": "06:00-10:00",
        "slot_news": "20:00-23:00",
        "slot_news_enabled": "true",
        "news_source": "legacy",   # 无对应新键的历史遗留键：忽略
    }, monkeypatch)
    cfg = _get_settings_config(monkeypatch)
    assert cfg["slot_free"] == "20:00-23:00"
    assert cfg["slot_free_enabled"] is True
    assert cfg["slot_weather"] == "06:00-10:00"


def test_settings_new_key_wins_over_legacy(monkeypatch):
    """文件同时含新旧键 → 回显新键优先、旧键兜底的语义不变。"""
    _write_runtime_config({
        "slot_news": "20:00-23:00", "slot_news_enabled": True,
        "slot_free": "22:00-24:00", "slot_free_enabled": False,
    }, monkeypatch)
    cfg = _get_settings_config(monkeypatch)
    assert cfg["slot_free"] == "22:00-24:00"
    assert cfg["slot_free_enabled"] is False


def test_settings_echo_typed_after_put(monkeypatch):
    """保存后回显按注册处声明的类型返回：开关真布尔、时段串为字符串。"""
    r = client.put("/api/settings", json={
        "free_enabled": False, "slot_free": "21:30-24:00", "slot_free_enabled": True,
    })
    assert r.status_code == 200, r.text
    cfg = _get_settings_config(monkeypatch)
    assert cfg["free_enabled"] is False
    assert cfg["slot_free_enabled"] is True
    assert cfg["slot_free"] == "21:30-24:00"


# ---------- 内容源契约测试（T7 #17 收口：每源四断言全套，ADR-0002） ----------

def _set_single_slot(slot_type: str) -> None:
    """固定全天时段为单一类型（runtime_config 白名单路径，与 /content 同源）。"""
    from app import runtime_config
    runtime_config.save({"slot_segments": [{"start": "00:00", "type": slot_type}]})


# 五种内容源（ADR-0002）：display/daily/weather/free 带前缀，library 为
# 无前缀兑底源（id 是查找键，进清单走推送注入而非源 meta()）
_SOURCE_CASES = ["daily", "weather", "free", "display", "library"]


def _prepare_source(monkeypatch, prefix: str) -> tuple[str, dict]:
    """按既有流程（上传图片 / 置顶上传 / 桩外部依赖 / 时段配置）产出
    「当前有效 id」与「元数据条目」（T7 #17 契约四断言之四）。

    display 经置顶显示上传（优先于一切时段内容，/content 即返回其条目）；
    library 经普通上传（元数据即上传响应；内容库没有「当前内容」概念，
    进清单走推送注入）；其余三源经时段配置 + /content。
    """
    _clear_display()
    if prefix == "display":
        r = client.post("/api/images/display/upload",
                        files={"file": ("disp.png", _png_bytes(1600, 1200, (198, 40, 40)), "image/png")})
        assert r.status_code == 200, r.text
    elif prefix == "library":
        r = client.post("/api/images/upload",
                        files={"file": ("lib.png", _png_bytes(color=(40, 128, 60)), "image/png")})
        assert r.status_code == 200, r.text
        meta = r.json()
        return meta["id"], meta
    elif prefix == "daily":
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
    return images[0]["id"], images[0]


@pytest.mark.parametrize("prefix", _SOURCE_CASES)
def test_content_source_contract(monkeypatch, prefix):
    """参数化契约测试收口（T2 #12 三源 → T3 #13 五源 → T4 #14 预览泛化 →
    T7 #17 收口）：每源四断言全套——固定路由退役后，本套即各源对外
    契约的全部断言（新源接入自动继承全套）。

    1. 前缀分发命中：内容指纹 id 经 /{id}/raw 返回 200（不误落其他源 404）；
    2. raw 可过帧解析：parse_fps6 校验 magic / 长度公式 / 尺寸通过；
    3. 预览由原图而来：统一原图缩放 ~800px JPEG（缺原图 → 404 的另一半
       由各源专项用例锁：daily/library/free/display/weather 各有缺原图用例）；
    4. 元数据字段齐全：清单条目（display/daily/weather/free）或上传响应
       元数据（library）字段集与契约一致（逐字段基准见 T5 #15 对比测试）。
    """
    from app.epd_image import parse_fps6
    content_id, meta_item = _prepare_source(monkeypatch, prefix)

    if prefix != "library":
        assert content_id.startswith(prefix + "-"), content_id

    # 1) 前缀分发命中：/{id}/raw 返回当前内容的帧
    dispatched = client.get(f"/api/images/{content_id}/raw")
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.headers["content-type"] == "application/octet-stream"

    # 2) raw 可过帧解析（oracle 用领域函数，不用内部接口）
    prepared = parse_fps6(dispatched.content)
    assert (prepared.width, prepared.height) == (1200, 1600)

    # 3) 预览由原图而来：原图缩放 ~800px JPEG
    prev = client.get(f"/api/images/{content_id}/preview")
    assert prev.status_code == 200, prev.text
    assert prev.headers["content-type"].startswith("image/jpeg")
    pimg = Image.open(io.BytesIO(prev.content))
    assert max(pimg.size) <= 800

    # 4) 元数据字段齐全
    if prefix == "library":
        # 内容库无「当前内容」概念：元数据即上传响应（进清单走推送注入）
        assert {"id", "filename", "width", "height", "landscape", "created_at",
                "raw_url", "preview_url"} <= set(meta_item), set(meta_item)
    else:
        assert set(meta_item) == _MANIFEST_FIELD_SETS[prefix], \
            f"{prefix}: 元数据字段集与契约不一致: {set(meta_item) ^ _MANIFEST_FIELD_SETS[prefix]}"

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


# ADR-0002 退役的 10 条固定 raw/preview 路由
# （每日精选/旧称新闻/自由模块/天气/置顶显示 × raw/preview；与 test_webui 的
#  _FIXED_ROUTES 同一张表——服务端删路由，前端零引用）
_RETIRED_FIXED_ROUTES = [
    "/api/images/daily/raw", "/api/images/daily/preview",
    "/api/images/news/raw", "/api/images/news/preview",
    "/api/images/free/raw", "/api/images/free/preview",
    "/api/images/weather/raw", "/api/images/weather/preview",
    "/api/images/display/raw", "/api/images/display/preview",
]


def test_fixed_raw_preview_routes_retired_404(monkeypatch):
    """固定 raw/preview 路由退役（T7 #17，ADR-0002）：十条全部 404。

    各源内容照常可达——统一走 /{id}/raw|preview 泛化路由（内容源注册表
    分发）；news- 前缀的旧固件兜底保留在泛化路由（见上一用例）。先备齐
    各源当前内容（未退役前这些路由返回 200，退役后与状态无关恒 404）。
    """
    r = client.post("/api/images/upload",
                    files={"file": ("retire.png", _png_bytes(color=(60, 60, 200)), "image/png")})
    assert r.status_code == 200, r.text          # 每日精选可渲染
    _mock_weather(monkeypatch)                    # 天气卡片可渲染
    _mock_free_degrade(monkeypatch)               # 自由模块可渲染（降级文字卡）
    _clear_display()
    r = client.post("/api/images/display/upload",
                    files={"file": ("retire_disp.png", _png_bytes(1600, 1200), "image/png")})
    assert r.status_code == 200, r.text           # 置顶显示有当前内容

    for path in _RETIRED_FIXED_ROUTES:
        assert client.get(path).status_code == 404, path

    _clear_display()


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


# ---------- 照片分类（#25：tab / 时段绑定 / 选片不出圈） ----------

def _seed_category_photo(path: str, memory: float, category_name: str, filename: str = "") -> None:
    """直接写一条带分类的 photo_scores 记录（隔离照片库路径断言）。"""
    from app import category, db
    import os
    db.upsert_photo_score(path, filename=filename or os.path.basename(path),
                          memory_score=memory, beauty_score=50, shot_at=None,
                          shot_source="file", analyzed_at=db.now(),
                          category=category_name,
                          content_hash=category.compute_content_hash(path))


def test_scores_filter_by_category_and_summary(monkeypatch):
    """S2：评分列表支持按分类过滤，并返回分类汇总（名称+照片数，tab 排序）。"""
    from app import category as cat_mod
    from app.config import settings as settings_mod
    monkeypatch.setattr(settings_mod, "photo_lib_dir", "/tmp/nonexistent-photolib")
    # 用真实存在的临时图片路径（上传目录隔离，不占照片库）
    import tempfile, os
    lib = tempfile.mkdtemp(prefix="cat-api-")
    _seed_category_photo(os.path.join(lib, "a.jpg"), 90, "宝宝", "a.jpg")
    _seed_category_photo(os.path.join(lib, "b.jpg"), 70, "宝宝", "b.jpg")
    _seed_category_photo(os.path.join(lib, "c.jpg"), 80, "婚礼视频", "c.jpg")

    r = client.get("/api/analysis/scores?limit=500&category=宝宝")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scores"] and all(i["category"] == "宝宝" for i in body["scores"])
    # 汇总含各分类与照片数
    cats = {c["name"]: c["count"] for c in body["summary"]}
    assert cats.get("宝宝", 0) >= 2
    assert cats.get("婚礼视频", 0) >= 1


def test_scores_unknown_category_empty(monkeypatch):
    """S2：请求不存在分类 → 返回空列表（前端据此显示空 tab）。"""
    r = client.get("/api/analysis/scores?limit=500&category=不存在分类")
    assert r.status_code == 200, r.text
    assert r.json()["scores"] == []


def test_settings_bind_valid_category_and_reject_missing(monkeypatch):
    """S2：编排绑定现存分类合法；绑定不存在分类 → 400 拒绝保存。"""
    # 现存分类：先种一条照片（category 域已隔离），确保 list_categories 命中
    from app import category as cat_mod
    from app.config import settings as settings_mod
    import tempfile, os
    monkeypatch.setattr(settings_mod, "photo_lib_dir", "/tmp/nonexistent")
    lib = tempfile.mkdtemp(prefix="set-cat-")
    _seed_category_photo(os.path.join(lib, "x.jpg"), 70, "宝宝", "x.jpg")
    existing = set(cat_mod.list_categories())
    assert "宝宝" in existing

    # 合法：绑定现存分类
    r = client.put("/api/settings", json={"slot_segments": [
        {"start": "00:00", "type": "photo", "category": "宝宝"},
        {"start": "08:00", "type": "free"},
    ]})
    assert r.status_code == 200, r.text
    segs = r.json()["saved"]["slot_segments"]
    assert any(s.get("category") == "宝宝" for s in segs)

    # 非法：绑定不存在分类 → 400
    r = client.put("/api/settings", json={"slot_segments": [
        {"start": "00:00", "type": "photo", "category": "不存在分类"},
    ]})
    assert r.status_code == 400, r.text
    assert "不存在" in r.json()["detail"]


def test_settings_bind_disk_folder_category_even_unanalyzed(monkeypatch):
    """照片库磁盘上真实存在但尚未分析的文件夹（如 NAS 新同步来的分类）
    也应可被时段绑定——分类列表由照片记录聚合 + 磁盘目录补足（ADR 需求 14）。"""
    from app import category as cat_mod
    from app.config import settings as settings_mod
    import os
    disk_lib = tempfile.mkdtemp(prefix="disk-cat-")
    os.makedirs(os.path.join(disk_lib, "宝宝成长记录"), exist_ok=True)   # 磁盘文件夹，无分析记录
    monkeypatch.setattr(settings_mod, "photo_lib_dir", disk_lib)
    monkeypatch.setattr(cat_mod.settings, "photo_lib_dir", disk_lib)
    r = client.put("/api/settings", json={"slot_segments": [
        {"start": "00:00", "type": "photo", "category": "宝宝成长记录"},
    ]})
    assert r.status_code == 200, r.text


def test_content_manifest_per_slot_bound_category(monkeypatch):
    """S2：照片时段绑定分类后，内容清单返回该分类内选出的每日精选（不出圈）。

    直接 monkeypatch daily 选片返回固定照片，验证 /content 把该照片的
    清单字段透传（id=daily-* 指纹）。"""
    from app import daily as daily_mod, runtime_config
    _set_single_slot("photo")
    runtime_config.save({"slot_segments": [{"start": "00:00", "type": "photo", "category": "宝宝"}]})
    import tempfile, os
    lib = tempfile.mkdtemp(prefix="slot-cat-")
    photo = os.path.join(lib, "pick.png")
    with open(photo, "wb") as f:
        f.write(_png_bytes(color=(200, 60, 60)))
    # 桩选片：直接返回该照片，渲染时按 key 落盘
    monkeypatch.setattr(daily_mod, "select_daily_photo",
                        lambda category=None: ({"path": photo, "filename": "pick.png",
                                                "caption": "宝宝", "memory_score": None}, True))
    r = client.get("/api/images/content")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "daily"
    assert data["images"][0]["id"].startswith("daily-")
    assert data["images"][0]["filename"] == "pick.png"
