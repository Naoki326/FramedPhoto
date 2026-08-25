"""FramedPhoto 服务端入口。"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from app.config import settings
from app.routers import analysis, calibration, devices, images, ota
from app.routers import settings as settings_router
from app.weather_lookup import router as weather_lookup_router

# ── 应用日志落盘（services/framedphoto.log，与 launchd 日志一致）──
# uvicorn 默认只配置自身 logger；这里显式配置 root，让 app.* 的
# WARNING/INFO（自由模块生成异常、入库失败等）可靠写入日志文件。
_log_path = Path(__file__).resolve().parent.parent / "framedphoto.log"
_log_handler = RotatingFileHandler(_log_path, maxBytes=5 * 1024 * 1024, backupCount=3,
                                   encoding="utf-8")
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                    handlers=[_log_handler], force=True)
# 保留 uvicorn 自身日志走原有 handler（stderr / access）
logging.getLogger("uvicorn").propagate = False

app = FastAPI(
    title="FramedPhoto Service",
    description="E Ink 数字相框：图片转换 / 设备管理 / OTA 分发",
    version="0.3.0",
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
app.include_router(analysis.router, prefix="/api/analysis", tags=["analysis"])
app.include_router(settings_router.router, prefix="/api/settings", tags=["settings"])
app.include_router(weather_lookup_router, prefix="/api/weather", tags=["weather"])
app.include_router(calibration.router, prefix="/api/calibration", tags=["calibration"])


@app.get("/", response_class=HTMLResponse, tags=["web"])
async def index():
    html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/api.js", tags=["web"])
async def api_js():
    """管理台 api() helper（#21 独立 seam，页面以 <script src> 引用）。"""
    return FileResponse(Path(__file__).parent / "web" / "api.js",
                        media_type="text/javascript")


@app.get("/health", tags=["meta"])
async def health():
    return {
        "status": "ok",
        "epd": f"{settings.epd_width}x{settings.epd_height}",
    }
