"""应用配置：从环境变量 / .env 读取。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_host: str = "0.0.0.0"
    app_port: int = 8000

    epd_width: int = 1200
    epd_height: int = 1600

    upload_dir: str = "./uploads"
    ota_dir: str = "./ota"
    db_path: str = "./framedphoto.db"
    log_level: str = "INFO"

    # AI 照片分析（VLM，OpenAI 兼容接口；不配置则走启发式评分）
    vlm_enabled: bool = True
    vlm_provider: str = "auto"     # auto | openai | anthropic | disabled
    vlm_api_url: str = ""          # OpenAI 兼容接口（LM Studio / 云端）
    vlm_api_key: str = ""
    vlm_model: str = "qwen3-vl-32b-instruct"
    vlm_timeout: int = 600

    # Anthropic（Claude 视觉）
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    anthropic_base_url: str = "https://api.anthropic.com"

    # 每日精选
    photo_lib_dir: str = "./photos"      # 照片库目录（analyze_photos 扫描）
    daily_min_score: float = 55.0          # 每日选片回忆度阈值
    daily_photo_quantity: int = 1          # 每日生成精选张数（设备取第一张）
    memory_threshold: float = 55.0         # 启发式回忆度阈值别名（保留兼容）


settings = Settings()
