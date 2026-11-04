from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "qsa"
    app_env: str = "dev"
    api_prefix: str = "/api"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://qsa:qsa@127.0.0.1:5432/qsa"
    redis_url: str = "redis://127.0.0.1:6379/0"
    use_fake_redis: bool = False

    cors_origins: str = "http://localhost:5173"
    session_cookie_name: str = "qsa_session"
    session_cookie_secure: bool = False
    session_cookie_samesite: str = "lax"

    settings_channel: str = "qsa:settings:changed"
    settings_poll_seconds: int = 30

    qq_bot_app_id: str = ""
    qq_bot_app_secret: str = ""
    qq_bot_api_base: str = "https://api.bot.qq.com"
    qq_bot_public_base_url: str = "http://127.0.0.1:5173"

    # 个人号（OneBot v11）通道的环境变量兜底，系统设置优先。
    personal_qq_ws_url: str = ""
    personal_qq_http_url: str = ""
    personal_qq_access_token: str = ""
    personal_qq_self_qq: str = ""

    history_dir: str = ""
    history_auto_import: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
