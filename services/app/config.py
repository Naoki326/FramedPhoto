"""应用配置：从环境变量 / .env 读取。"""
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

    # 每日精选
    photo_lib_dir: str = "./photos"      # 照片库目录（analyze_photos 扫描）
    daily_min_score: float = 55.0          # 每日选片回忆度阈值
    daily_photo_quantity: int = 1          # 每日生成精选张数（设备取第一张）
    memory_threshold: float = 55.0         # 启发式回忆度阈值别名（保留兼容）

    # 时段编排（24h 三时段：天气卡片 / 每日照片 / 新闻卡片，HH:MM-HH:MM）
    slot_weather: str = "00:00-10:00"
    slot_photo: str = "10:00-21:00"
    slot_news: str = "21:00-24:00"
    slot_weather_enabled: bool = True
    slot_news_enabled: bool = True
    news_source: str = "60s"          # zhipu | 60s

    # 天气（和风天气）
    qweather_key: str = ""
    qweather_location: str = "101010100"   # 城市 ID（北京），可在和风控制台查询

    # 新闻（智谱 GLM web search）
    zhipu_api_key: str = ""
    zhipu_model: str = "glm-4-flash"

    # 文生图（用户提供，兼容 OpenAI 图片接口或自定义）
    imagegen_url: str = ""
    imagegen_key: str = ""
    imagegen_model: str = ""


settings = Settings()
