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
    calendar_cache_dir: str = str(Path(__file__).resolve().parent / "calendar_cache")
    ip_loc_cache_file: str = ""    # 留空 = WEATHER_CACHE_DIR/ip_loc.json

    # 浓彩偏置：色域外高饱和色的墨水对混合向彩色端偏移（0 = 忠实混色，
    # 上限 4；管理台校准页清杆读写，见 ADR-0004）
    chroma_bias: float = 0.0

    # AI 照片分析（VLM）
    vlm_enabled: bool = True
    vlm_provider: str = "auto"     # auto | openai | anthropic | disabled
    vlm_api_mode: str = "chat"     # chat(chat/completions) | responses(/responses)
    vlm_api_url: str = ""          # OpenAI 兼容接口（LM Studio / 云端 / 中转站）
    vlm_api_key: str = ""
    vlm_model: str = "qwen3.8-max"
    vlm_timeout: int = 600
    vlm_proxy: str = ""            # HTTP(S) 代理，如 http://127.0.0.1:7897（绕过区域限制时用）

    # Anthropic 兼容（Claude 视觉）
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    anthropic_base_url: str = "https://api.anthropic.com"

    # NAS（照片库主存储：pull 由 sync_nas.sh 定时/手动执行；
    # push 与远端文件夹操作见 app/nas_sync.py，ADR-0007）
    nas_ssh_host: str = ""
    nas_ssh_user: str = ""
    nas_ssh_port: int = 22
    nas_photo_dir: str = "/volume1/photo"
    nas_rsync_path: str = ""
    nas_rsync_bwlimit: int = 0

    # 每日精选
    photo_lib_dir: str = "./photos"      # 照片库目录（NAS 镜像：同步 pull + 上传 push）
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

    # 天气（和风天气；厂家可通过 weather_* 键整体替换）
    weather_key: str = ""
    weather_host: str = "devapi.qweather.com"   # 专属 API Host（控制台项目设置，如 xxx.re.qweatherapi.com）
    weather_location: str = "101010100"
    weather_city: str = ""                 # 城市名（可选，显示在卡片底部）

    # 自由模块（LLM + 文生图每日生成，替代原新闻卡片）
    free_enabled: bool = True
    free_rotate: bool = True   # 每天轮换启用中的模块

    # 文本 LLM（自由模块文案 / 日历寄语 / 天气卡片设计）
    # 配置面按接口形态描述，不绑厂商：换厂家 = 换 base_url + key + model
    text_api: str = "openai"               # openai(chat/completions) | anthropic(/v1/messages)
    text_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    text_api_key: str = ""
    text_model: str = "glm-4-flash"

    # 文生图：OpenAI / 签名式异步 / Cloudflare
    #   openai        —— OpenAI 兼容图片接口（URL + KEY + MODEL，同步返回图）
    #   signed_async  —— 签名式异步任务接口（AK/SK，提交 + 轮询取图）
    image_api: str = "openai"
    image_api_url: str = ""
    image_api_key: str = ""
    image_model: str = ""
    image_signed_ak: str = ""
    image_signed_sk: str = ""
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    image_ratio: str = "4:3"   # 横屏（相框横放观看）

    # ── 旧键兼容（迁移期：.env 里历史的厂商名键仍能读到）──
    # 新键优先；此处仅在 .env/环境变量里提供了旧键时生效（不动 runtime_config，
    # 那里的旧键由 ALLOWED_KEYS 别名负责）。随下次用户改 .env 时自然淘汰。
    zhipu_api_key: str = ""
    zhipu_model: str = ""
    jimeng_access_key: str = ""
    jimeng_secret_key: str = ""
    jimeng_ratio: str = ""
    imagegen_url: str = ""
    imagegen_key: str = ""
    imagegen_model: str = ""
    qweather_key: str = ""
    qweather_host: str = ""
    qweather_location: str = ""
    qweather_city: str = ""

    def model_post_init(self, __context: Any) -> None:
        # IP 定位缓存默认随天气卡片缓存目录（沿用原私有实现 CACHE_DIR/ip_loc.json
        # 的耦合：只改 WEATHER_CACHE_DIR 时它跟着走，显式设置则独立）
        if not self.ip_loc_cache_file:
            self.ip_loc_cache_file = str(Path(self.weather_cache_dir) / "ip_loc.json")
        self._apply_legacy_aliases()

    def _apply_legacy_aliases(self) -> None:
        """旧厂商名键 → 中性键。

        `.env` 里历史的 ZHIPU_* / JIMENG_* / IMAGEGEN_* / QWEATHER_* 仍能读到，
        用户无需立刻改配置；判定用 model_fields_set（“本次是否显式提供了
        新键”），这样才能区分「新键没写，只是有默认值」与「用户真的设了新键」
        —— 否则 text_model 这类非空默认值会把旧键挡掉。
        厂商专属键不进入管理台 schema，也不写入 runtime_config（那边由
        runtime_config.ALIASES 处理历史存量）。
        """
        provided = self.model_fields_set
        for new_key, legacy_key in (
            ("text_api_key", "zhipu_api_key"),
            ("text_model", "zhipu_model"),
            ("image_signed_ak", "jimeng_access_key"),
            ("image_signed_sk", "jimeng_secret_key"),
            ("image_ratio", "jimeng_ratio"),
            ("image_api_url", "imagegen_url"),
            ("image_api_key", "imagegen_key"),
            ("image_model", "imagegen_model"),
            ("weather_key", "qweather_key"),
            ("weather_host", "qweather_host"),
            ("weather_location", "qweather_location"),
            ("weather_city", "qweather_city"),
        ):
            if new_key not in provided and getattr(self, legacy_key, ""):
                setattr(self, new_key, getattr(self, legacy_key))
        self._infer_image_provider()

    def _infer_image_provider(self) -> None:
        """未显式指定 image_api 时，按已配凭据推出一条文生图链路。

        优先级：显式 IMAGE_API > 只配了签名式凭据（AK/SK）> 默认 OpenAI 兼容。
        旧 .env 里只配了 JIMENG_* 时自动落到签名式，旧配置不因改名而失效；
        两条链路都配时以显式 IMAGE_API 为准，不在这里猜测。
        """
        if "image_api" in self.model_fields_set:
            return
        signed = bool(self.image_signed_ak or self.image_signed_sk)
        openai = bool(self.image_api_url or self.image_api_key or self.image_model)
        if signed and not openai:
            self.image_api = "signed_async"


settings = Settings()
