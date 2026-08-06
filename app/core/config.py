from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """BluePay runtime configuration.

    Railway's ``DATABASE_URL`` is optional for local development, but when it
    is present PostgreSQL becomes the primary and only writable datastore.
    Both Railway's public and private PostgreSQL URL formats are accepted.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    bot_token: str = Field(alias="BOT_TOKEN")
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    database_private_url: str | None = Field(default=None, alias="DATABASE_PRIVATE_URL")
    explicit_base_url: str = Field(default="", alias="BASE_URL")
    port: int = Field(default=8080, alias="PORT")
    default_fee_rial: int = Field(default=20_000, alias="DEFAULT_FEE_RIAL")
    invoice_ttl_minutes: int = Field(default=30, alias="INVOICE_TTL_MINUTES")
    portal_secret: str | None = Field(default=None, alias="PORTAL_SECRET")
    sentry_dsn: str | None = Field(default=None, alias="SENTRY_DSN")
    environment: str = Field(default="production", alias="APP_ENV")
    callback_worker_interval_seconds: int = Field(default=2, alias="CALLBACK_WORKER_INTERVAL_SECONDS")
    callback_worker_batch_size: int = Field(default=30, alias="CALLBACK_WORKER_BATCH_SIZE")
    telegram_mode: str = Field(default="polling", alias="TELEGRAM_MODE")
    telegram_webhook_secret: str | None = Field(default=None, alias="TELEGRAM_WEBHOOK_SECRET")
    sms_hmac_max_age_seconds: int = Field(default=300, alias="SMS_HMAC_MAX_AGE_SECONDS")
    release_staging_branch: str = Field(default="bluepay-staging", alias="RELEASE_STAGING_BRANCH")
    startup_fail_open: bool = Field(default=False, alias="STARTUP_FAIL_OPEN")
    auto_import_legacy_sqlite: bool = Field(default=True, alias="AUTO_IMPORT_LEGACY_SQLITE")

    @field_validator("explicit_base_url", mode="before")
    @classmethod
    def normalize_base_url(cls, value: str | None) -> str:
        return (value or "").strip().rstrip("/")

    @property
    def github_repository(self) -> str:
        owner = os.getenv("RAILWAY_GIT_REPO_OWNER", "").strip()
        name = os.getenv("RAILWAY_GIT_REPO_NAME", "").strip()
        if owner and name:
            return f"{owner}/{name}"
        fallback = os.getenv("GITHUB_REPOSITORY", "").strip()
        if fallback and "/" in fallback:
            return fallback
        return fallback or "unknown/unknown"

    @property
    def github_branch(self) -> str:
        return os.getenv("RAILWAY_GIT_BRANCH", "").strip() or "main"

    @property
    def data_branch(self) -> str:
        return "gateway-data"

    @property
    def data_dir(self) -> Path:
        path = Path(os.getenv("GATEWAY_RUNTIME_DIR", "/app/runtime" if Path("/app").exists() else "runtime"))
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def database_path(self) -> Path:
        return self.data_dir / "gateway.db"

    @property
    def legacy_database_path(self) -> Path:
        return self.data_dir / "legacy-gateway.db"

    @property
    def raw_database_url(self) -> str:
        # DATABASE_URL is the preferred Railway reference. DATABASE_PRIVATE_URL
        # is accepted as a fallback for projects that expose that variable.
        return (self.database_url or self.database_private_url or "").strip()

    @property
    def effective_database_url(self) -> str:
        raw = self.raw_database_url
        if not raw:
            return f"sqlite+aiosqlite:///{self.database_path}"
        if "${{" in raw or "}}" in raw:
            raise ValueError(
                "DATABASE_URL was not resolved by Railway. Set it to "
                "${{Postgres.DATABASE_URL}} from the BluePay service Variables page."
            )
        if raw.startswith("postgres://"):
            raw = "postgresql://" + raw[len("postgres://"):]
        if raw.startswith("postgresql://"):
            raw = "postgresql+asyncpg://" + raw[len("postgresql://"):]
        elif raw.startswith("postgresql+psycopg://"):
            raw = "postgresql+asyncpg://" + raw[len("postgresql+psycopg://"):]
        if not raw.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite:///")):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL supplied by Railway")
        return raw

    @property
    def normalized_database_url(self) -> str:
        return self.effective_database_url

    @property
    def is_postgres(self) -> bool:
        return self.effective_database_url.startswith("postgresql+asyncpg://")

    @property
    def database_mode(self) -> str:
        return "postgresql" if self.is_postgres else "sqlite"

    @property
    def base_url(self) -> str:
        if self.explicit_base_url:
            return self.explicit_base_url
        railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if railway_domain:
            return f"https://{railway_domain}"
        return f"http://localhost:{self.port}"

    @property
    def effective_portal_secret(self) -> str:
        return self.portal_secret or self.bot_token

    @property
    def effective_telegram_webhook_secret(self) -> str:
        import hashlib
        raw = (self.telegram_webhook_secret or f"bluepay:{self.bot_token}").encode()
        return hashlib.sha256(raw).hexdigest()

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.base_url}/webhooks/telegram/{self.effective_telegram_webhook_secret[:32]}"

    @property
    def use_telegram_webhook(self) -> bool:
        mode = (self.telegram_mode or "polling").strip().lower()
        if mode == "webhook":
            return True
        if mode == "polling":
            return False
        return self.base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
