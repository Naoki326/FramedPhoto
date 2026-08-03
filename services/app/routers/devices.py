"""
devices.py — 设备注册 / 心跳 / 状态上报。

M3 完整实现：设备注册（返回 device_id）、心跳带状态（固件版本、电量、
当前显示内容 id），服务端据此编排内容推送与 OTA。
当前为骨架，数据存于进程内存（后续换 SQLite / Postgres）。
"""
from fastapi import APIRouter, HTTPException

router = APIRouter()

# 内存存储占位：device_id -> status dict（重启丢失，M3 迁移持久化）
_registry: dict[str, dict] = {}


@router.post("/register")
async def register(body: dict):
    device_id = body.get("device_id") or body.get("mac")
    if not device_id:
        raise HTTPException(400, "device_id or mac required")
    _registry[device_id] = {"registered_at": None, "last_seen": None, **body}
    return {"device_id": device_id, "ok": True}


@router.post("/{device_id}/heartbeat")
async def heartbeat(device_id: str, body: dict):
    if device_id not in _registry:
        raise HTTPException(404, "unknown device, register first")
    _registry[device_id].update(body)
    return {"ok": True, "device_id": device_id}


@router.get("")
async def list_devices():
    return _registry
