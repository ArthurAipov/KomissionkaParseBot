import logging
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = "postgresql+psycopg://watcher:watcher@localhost:5432/watcher"
    api_key: SecretStr
    bot_token: SecretStr = SecretStr("")
    telegram_proxy_url: SecretStr = SecretStr("")
    api_base_url: str = "http://localhost:8000"
    scan_interval_seconds: int = Field(1800, ge=300)
    request_delay_seconds: float = Field(5, ge=5)
    request_jitter_seconds: float = Field(2, ge=0)
    requests_per_hour: int = Field(240, ge=1, le=600)
    snapshot_limit: int = Field(15, ge=1, le=20)
    page_cache_seconds: int = Field(300, ge=60)
    block_cooldown_seconds: int = Field(21600, ge=1800)
    respect_robots: bool = False
    crawler_user_agent: str = "KomissionkiWatcher/0.1"
    log_level: str = "INFO"
    photo_cache_dir: str = ".media-cache"
    photo_cache_max_mb: int = Field(256, ge=16, le=4096)
    photo_cache_ttl_seconds: int = Field(604800, ge=3600)
    admin_telegram_ids: str = ""
    crawl_pages_per_turn: int = Field(3, ge=1, le=10)
    backups_dir: str = ".backups"
    host_disk_path: str = "."
    disk_warn_free_mb: int = Field(2048, ge=128)

    @field_validator("telegram_proxy_url")
    @classmethod
    def valid_telegram_proxy(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            return value
        try:
            url = urlsplit(value.get_secret_value())
            valid = (
                url.scheme in {"http", "socks4", "socks5"}
                and url.hostname
                and url.port
                and url.path in {"", "/"}
                and not url.query
                and not url.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "TELEGRAM_PROXY_URL must be an HTTP/SOCKS proxy URL with host and port"
            )
        return value

    @property
    def admin_ids(self) -> set[int]:
        return {int(x.strip()) for x in self.admin_telegram_ids.split(",") if x.strip()}

    @field_validator("admin_telegram_ids")
    @classmethod
    def valid_admins(cls, value: str) -> str:
        if value and any(not x.strip().isdigit() or int(x.strip()) <= 0 for x in value.split(",")):
            raise ValueError(
                "ADMIN_TELEGRAM_IDS must contain positive numeric IDs separated by commas"
            )
        return value

    @property
    def telegram_bot_id(self) -> int:
        token = self.bot_token.get_secret_value()
        return int(token.split(":", 1)[0]) if token else 0

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 24 or value.get_secret_value().startswith("replace-"):
            raise ValueError("API_KEY must be a random secret of at least 24 characters")
        return value


@lru_cache
def settings() -> Settings:
    return Settings()


def configure_logging() -> None:
    logging.basicConfig(
        level=settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # HTTP client URLs may contain a bot token; do not enable their request logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
