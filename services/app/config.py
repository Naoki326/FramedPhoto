"""应用配置：从环境变量 / .env 读取。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_host: str = "0.0.0.0"
    app_port: int = 8000

    epd_width: int = 1200
    epd_height: int = 1600

    upload_dir: str = "./uploads"
    log_level: str = "INFO"


settings = Settings()
