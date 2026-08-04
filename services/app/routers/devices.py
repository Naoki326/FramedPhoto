"""
devices.py — 设备注册 / 心跳 / 状态查询（SQLite 持久化）。

- POST /register         注册设备（device_id 或 mac），返回注册信息
- POST /{id}/heartbeat   心跳：上报固件版本/电量/当前内容/IP，更新 last_seen；
                         响应携带待下发 WiFi 配置（wifi_pending 时）
- PUT  /{id}/wifi-config 管理台设置待下发 WiFi 配置（pending 置位）
- GET  ""                设备列表（按最近心跳排序）
- GET  /{id}             单设备详情
- GET  /local-wifi       本机（Mac）当前连接的 WiFi SSID/密码（管理台自动填充用）
"""
import logging
import shutil
import subprocess

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app import db, esptouch

logger = logging.getLogger(__name__)

router = APIRouter()

# ESPTouch v2 共享密钥（16 字节），须与固件 ESPTOUCH_V2_KEY 一致
ESPTOUCH_V2_KEY = b"FramedPhoto2024!"


class RegisterBody(BaseModel):
    device_id: str | None = None
    mac: str | None = None
    name: str = ""


@router.post("/register")
async def register(body: RegisterBody):
    dev_id = (body.device_id or body.mac or "").strip()
    if not dev_id:
        raise HTTPException(400, "device_id or mac required")
    db.upsert_device(dev_id, name=body.name, mac=body.mac or "", current_image="")
    dev = db.get_device(dev_id)
    return {"device_id": dev_id, "registered": True, "device": dev}


class HeartbeatBody(BaseModel):
    firmware_version: str = ""
    battery: float | None = None
    current_image: str = ""
    ip: str = ""
    uptime_s: int = Field(default=0, description="设备开机时长（秒）")
    wifi_config_applied: bool = Field(default=False, description="设备已应用上次下发的 WiFi 配置")


class WifiConfigBody(BaseModel):
    ssid: str = Field(min_length=1, max_length=64)
    password: str = Field(default="", max_length=64)


@router.post("/{device_id}/heartbeat")
async def heartbeat(device_id: str, body: HeartbeatBody):
    if not db.get_device(device_id):
        db.upsert_device(device_id)
    db.upsert_device(
        device_id,
        firmware_version=body.firmware_version,
        battery=body.battery if body.battery is not None else -1,
        current_image=body.current_image,
        ip=body.ip,
        last_seen=db.now(),
    )
    # 设备确认已应用 WiFi 配置 → 清 pending
    if body.wifi_config_applied:
        db.upsert_device(device_id, wifi_pending=0)
    # 心跳响应携带待下发配置
    resp: dict = {"ok": True, "device_id": device_id, "poll_interval_min": None}
    dev = db.get_device(device_id)
    if dev and dev.get("wifi_pending"):
        resp["wifi_config"] = {"ssid": dev.get("wifi_ssid", ""), "password": dev.get("wifi_password", "")}
    return resp


@router.put("/{device_id}/wifi-config")
async def set_wifi_config(device_id: str, body: WifiConfigBody):
    if not db.get_device(device_id):
        raise HTTPException(404, "unknown device")
    db.upsert_device(
        device_id,
        wifi_ssid=body.ssid,
        wifi_password=body.password,
        wifi_pending=1,
    )
    return {"ok": True, "device_id": device_id, "wifi_config": {"ssid": body.ssid}}


class SmartConfigBody(BaseModel):
    ssid: str = Field(min_length=1, max_length=64)
    password: str = Field(default="", max_length=64)


@router.post("/smartconfig")
async def smartconfig_provision(body: SmartConfigBody, bg: BackgroundTasks):
    """SmartConfig 配网：局域网广播 SSID/密码，设备（未关联 WiFi）监听并自动连接。
    全程不断网；后台发送约 6 秒。
    """
    bg.add_task(_send_smartconfig, body.ssid, body.password)
    return {"started": True, "ssid": body.ssid, "hint": "约 6 秒发送完成，设备收到后自动连接"}


def _send_smartconfig(ssid: str, password: str) -> None:
    try:
        # ESPTouch v2：AES 加密，广播 30s（实测兼容性好且同网嗅探无法还原密码）
        esptouch.send_smartconfig_v2(ssid, password, ESPTOUCH_V2_KEY, duration_s=30)
    except Exception:  # noqa: BLE001
        logger.exception("smartconfig send failed")


@router.get("/local-wifi")
async def local_wifi():
    """管理台辅助：返回本机（服务端所在机器）当前连接的 WiFi。
    密码尝试从 macOS 钥匙串读取，读不到时留空（用户手输）。
    """
    ssid = _current_wifi_ssid()
    password = _keychain_wifi_password(ssid) if ssid else ""
    return {"ssid": ssid or "", "password": password or ""}


def _current_wifi_ssid() -> str:
    for cmd in (["networksetup", "-getairportnetwork", "en0"],
                ["networksetup", "-getairportnetwork", "en1"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            line = out.stdout.strip().splitlines()
            if line and "Current Wi-Fi Network:" in line[0]:
                return line[0].split(":", 1)[1].strip()
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
    return ""


def _keychain_wifi_password(ssid: str) -> str:
    if not ssid or not shutil.which("security"):
        return ""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-wa", ssid],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except subprocess.SubprocessError:
        return ""


@router.get("")
async def list_devices():
    return {"devices": db.list_devices()}


@router.get("/{device_id}")
async def get_device(device_id: str):
    dev = db.get_device(device_id)
    if not dev:
        raise HTTPException(404, "unknown device")
    return dev
