from __future__ import annotations

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Zero-configuration runtime settings.

    Only BOT_TOKEN and GITHUB_TOKEN are supplied by the user. Railway injects
    repository, branch, domain and port metadata automatically for GitHub deploys.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    github_token: str
    port: int = 8080
    default_fee_rial: int = 20_000
    invoice_ttl_minutes: int = 30

    @property
    def github_repository(self) -> str:
        owner = os.getenv("RAILWAY_GIT_REPO_OWNER", "").strip()
        name = os.getenv("RAILWAY_GIT_REPO_NAME", "").strip()
        if owner and name:
            return f"{owner}/{name}"
        # Local development fallback; not shown as an installation setting.
        fallback = os.getenv("GITHUB_REPOSITORY", "").strip()
        if fallback and "/" in fallback:
            return fallback
        raise RuntimeError(
            "GitHub repository was not detected. Deploy the service from a GitHub repository on Railway."
        )

    @property
    def github_branch(self) -> str:
        return os.getenv("RAILWAY_GIT_BRANCH", "").strip() or "main"

    @property
    def data_branch(self) -> str:
        return "gateway-data"

    @property
    def data_dir(self) -> Path:
        # The container filesystem is restored from an encrypted GitHub snapshot
        # on every cold start, so no Railway Volume or DATABASE_URL is required.
        path = Path(os.getenv("GATEWAY_RUNTIME_DIR", "/app/runtime"))
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def database_path(self) -> Path:
        return self.data_dir / "gateway.db"

    @property
    def normalized_database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path}"

    @property
    def base_url(self) -> str:
        railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if railway_domain:
            return f"https://{railway_domain}"
        return f"http://localhost:{self.port}"


settings = Settings()
