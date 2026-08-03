"""
devices.py — 设备注册 / 心跳 / 状态查询（SQLite 持久化）。

- POST /register         注册设备（device_id 或 mac），返回注册信息
- POST /{id}/heartbeat   心跳：上报固件版本/电量/当前内容/IP，更新 last_seen
- GET  ""                设备列表（按最近心跳排序）
- GET  /{id}             单设备详情
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import db

router = APIRouter()


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
    # 心跳响应可携带服务端指令（如强制刷新内容）
    return {"ok": True, "device_id": device_id, "poll_interval_min": None}


@router.get("")
async def list_devices():
    return {"devices": db.list_devices()}


@router.get("/{device_id}")
async def get_device(device_id: str):
    dev = db.get_device(device_id)
    if not dev:
        raise HTTPException(404, "unknown device")
    return dev
