"""
ota.py — 固件发布与设备升级清单。

M3 完整实现：上传固件 bin、生成 manifest（版本 / sha256 / URL），
设备心跳上报固件版本后由服务端下发升级目标。
"""
from fastapi import APIRouter

router = APIRouter()

_firmware: dict | None = None


@router.post("/upload")
async def upload_firmware():
    # TODO(M3): 接收固件 bin，计算 sha256，生成带签名的 manifest
    return {"ok": False, "detail": "not implemented yet"}


@router.get("/manifest")
async def get_manifest(device_id: str | None = None):
    """设备轮询：返回当前可升级的固件信息（无则 null）。"""
    return {"firmware": _firmware}
