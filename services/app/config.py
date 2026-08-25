"""应用配置：从环境变量 / .env 读取。"""
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra=ignore：.env 中脚本用配置（NAS_SSH_* 等）不影响服务端加载
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "0.0.0.0"
    app_port: int = 8010

    epd_width: int = 1200
    epd_height: int = 1600

    upload_dir: str = "./uploads"
    ota_dir: str = "./ota"
    db_path: str = "./framedphoto.db"
    log_level: str = "INFO"

    # 渲染缓存路径（公开设置：env 可覆盖；默认值与原模块私有路径一致，
    # 测试隔离同 DB_PATH 模式，见 services/conftest.py）
    free_cache_dir: str = str(Path(__file__).resolve().parent / "free_cache")
    weather_cache_dir: str = str(Path(__file__).resolve().parent / "weather_cache")
    ip_loc_cache_file: str = ""    # 留空 = WEATHER_CACHE_DIR/ip_loc.json

    # 浓彩偏置：色域外高饱和色的墨水对混合向彩色端偏移（0 = 忠实混色，
    # 上限 4；管理台校准页清杆读写，见 ADR-0004）
    chroma_bias: float = 0.0

    # AI 照片分析（VLM）
    vlm_enabled: bool = True
    vlm_provider: str = "auto"     # auto | openai | anthropic | disabled
    vlm_api_mode: str = "chat"     # chat(chat/completions) | responses(/responses)
    vlm_api_url: str = ""          # OpenAI 兼容接口（LM Studio / 云端 / OpenCode Go）
    vlm_api_key: str = ""
    vlm_model: str = "qwen3.8-max"
    vlm_timeout: int = 600
    vlm_proxy: str = ""            # HTTP(S) 代理，如 http://127.0.0.1:7897（绕过区域限制时用）

    # Anthropic（Claude 视觉）
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    anthropic_base_url: str = "https://api.anthropic.com"

    # NAS（服务端 API 用于手动同步与内容库单图上传；pull 仍由 sync_nas.sh 执行）
    nas_ssh_host: str = ""
    nas_ssh_user: str = ""
    nas_ssh_port: int = 22
    nas_photo_dir: str = "/volume1/photo"
    nas_upload_dir: str = ""       # 留空 = NAS_PHOTO_DIR/FramedPhoto
    nas_rsync_path: str = ""
    nas_rsync_bwlimit: int = 0

    # 每日精选
    photo_lib_dir: str = "./photos"      # 照片库目录（analyze_photos 扫描）
    daily_min_score: float = 55.0          # 每日选片回忆度阈值
    daily_photo_quantity: int = 1          # 每日生成精选张数（设备取第一张）
    memory_threshold: float = 55.0         # 启发式回忆度阈值别名（保留兼容）

    # 天气卡片风格（auto=每日轮换 | apple | blue | magazine | classic）
    weather_style: str = "auto"

    # 时段编排（24h 三时段：天气卡片 / 每日照片 / 自由模块，HH:MM-HH:MM）
    slot_weather: str = "00:00-10:00"
    slot_photo: str = "10:00-21:00"
    slot_free: str = "21:00-24:00"
    slot_weather_enabled: bool = True
    slot_free_enabled: bool = True
    # 兼容旧键：历史 runtime_config 里可能存了 slot_news / slot_news_enabled / news_source
    slot_news: str = "21:00-24:00"
    slot_news_enabled: bool = True
    news_source: str = ""

    # 天气（和风天气）
    qweather_key: str = ""
    qweather_host: str = "devapi.qweather.com"   # 专属 API Host（控制台项目设置，如 xxx.re.qweatherapi.com）
    qweather_location: str = "101010100"
    qweather_city: str = ""                 # 城市名（可选，显示在卡片底部）   # 城市 ID（北京），可在和风控制台查询

    # 自由模块（LLM + 文生图每日生成，替代原新闻卡片）
    free_enabled: bool = True
    free_rotate: bool = True   # 每天轮换启用中的模块

    # 智谱 GLM（LLM 生成自由模块当日内容 + 天气卡片设计）
    zhipu_api_key: str = ""
    zhipu_model: str = "glm-4-flash"

    # 文生图（用户提供，兼容 OpenAI 图片接口或自定义）
    imagegen_url: str = ""
    imagegen_key: str = ""
    imagegen_model: str = ""

    # 即梦（火山引擎）文生图
    jimeng_access_key: str = ""
    jimeng_secret_key: str = ""
    jimeng_ratio: str = "4:3"   # 横屏（相框横放观看）

    def model_post_init(self, __context: Any) -> None:
        # IP 定位缓存默认随天气卡片缓存目录（沿用原私有实现 CACHE_DIR/ip_loc.json
        # 的耦合：只改 WEATHER_CACHE_DIR 时它跟着走，显式设置则独立）
        if not self.ip_loc_cache_file:
            self.ip_loc_cache_file = str(Path(self.weather_cache_dir) / "ip_loc.json")


settings = Settings()
