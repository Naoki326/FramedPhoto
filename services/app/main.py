"""FramedPhoto 服务端入口。"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import devices, images, ota

app = FastAPI(
    title="FramedPhoto Service",
    description="E Ink 数字相框：图片转换 / 设备管理 / OTA 分发",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 局域网设备访问，按需收紧
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(devices.router, prefix="/api/devices", tags=["devices"])
app.include_router(images.router, prefix="/api/images", tags=["images"])
app.include_router(ota.router, prefix="/api/ota", tags=["ota"])


@app.get("/health", tags=["meta"])
async def health():
    return {
        "status": "ok",
        "epd": f"{settings.epd_width}x{settings.epd_height}",
    }
