from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings with backward-compatible zero-config defaults.

    BOT_TOKEN and GITHUB_TOKEN remain sufficient for SQLite mode. Setting a
    Railway DATABASE_URL transparently upgrades the primary database to
    PostgreSQL without changing the API or bot configuration.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    bot_token: str = Field(alias="BOT_TOKEN")
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    db_require_postgres: bool = Field(default=False, alias="DB_REQUIRE_POSTGRES")
    db_connect_retries: int = Field(default=20, alias="DB_CONNECT_RETRIES")
    db_connect_retry_seconds: float = Field(default=3.0, alias="DB_CONNECT_RETRY_SECONDS")
    db_pool_size: int = Field(default=5, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=10, alias="DB_MAX_OVERFLOW")
    db_pool_timeout_seconds: int = Field(default=30, alias="DB_POOL_TIMEOUT_SECONDS")
    db_pool_recycle_seconds: int = Field(default=300, alias="DB_POOL_RECYCLE_SECONDS")
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

    @field_validator("explicit_base_url", mode="before")
    @classmethod
    def normalize_base_url(cls, value: str | None) -> str:
        return (value or "").strip().rstrip("/")

    @field_validator(
        "db_connect_retries",
        "db_pool_size",
        "db_max_overflow",
        "db_pool_timeout_seconds",
        "db_pool_recycle_seconds",
    )
    @classmethod
    def validate_non_negative_database_number(cls, value: int) -> int:
        return max(0, int(value))

    @field_validator("db_connect_retry_seconds")
    @classmethod
    def validate_database_retry_delay(cls, value: float) -> float:
        return max(0.25, min(float(value), 60.0))

    @model_validator(mode="after")
    def enforce_production_database(self) -> "Settings":
        raw = (self.database_url or "").strip()
        if self.db_require_postgres and not raw:
            raise ValueError(
                "DB_REQUIRE_POSTGRES=true but DATABASE_URL is missing. "
                "Set DATABASE_URL=${{Postgres.DATABASE_URL}} on the BluePay service."
            )
        if self.db_require_postgres:
            normalized = raw.replace("postgres://", "postgresql://", 1)
            if not normalized.startswith(("postgresql://", "postgresql+asyncpg://")):
                raise ValueError("DB_REQUIRE_POSTGRES=true requires a PostgreSQL DATABASE_URL")
        return self

    @property
    def github_repository(self) -> str:
        owner = os.getenv("RAILWAY_GIT_REPO_OWNER", "").strip()
        name = os.getenv("RAILWAY_GIT_REPO_NAME", "").strip()
        if owner and name:
            return f"{owner}/{name}"
        fallback = os.getenv("GITHUB_REPOSITORY", "").strip()
        if fallback and "/" in fallback:
            return fallback
        # PostgreSQL mode does not require GitHub database storage, but update
        # publishing can still use the repository metadata when available.
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
    def effective_database_url(self) -> str:
        raw = (self.database_url or "").strip()
        if not raw:
            return f"sqlite+aiosqlite:///{self.database_path}"
        if raw.startswith("postgres://"):
            raw = "postgresql://" + raw[len("postgres://"):]
        if raw.startswith("postgresql://"):
            raw = "postgresql+asyncpg://" + raw[len("postgresql://"):]
        return raw

    @property
    def normalized_database_url(self) -> str:
        return self.effective_database_url

    @property
    def is_postgres(self) -> bool:
        return self.effective_database_url.startswith("postgresql+asyncpg://")

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
        """Enable webhook only when it is explicitly requested.

        Railway rolling deploys and generated domains can temporarily leave a
        webhook pointing at an old/unready replica.  Polling is therefore the
        zero-configuration default for a single-replica BluePay service.  The
        legacy value ``auto`` is intentionally treated as polling so existing
        installations recover without adding or changing a variable.
        """

        mode = (self.telegram_mode or "polling").strip().lower()
        return mode == "webhook"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
