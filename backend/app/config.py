"""Application configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-only-secret-change-me-dev-only-secret-change-me"  # noqa: S105
_ENV_FILES = (Path(__file__).resolve().parents[2] / ".env", Path(".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES, extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    app_name: str = "ASEA"
    log_level: str = "INFO"
    log_json: bool = False

    database_url: str = "postgresql+psycopg://asea:asea@localhost:55432/asea"

    # --- auth ---
    jwt_secret: SecretStr = SecretStr(_DEV_SECRET)
    access_token_minutes: int = 30
    refresh_token_days: int = 7
    allow_registration: bool = True
    cors_origins: str = "http://localhost:5173"
    login_max_attempts: int = 10
    login_window_seconds: int = 300

    # --- approvals and credentials ---
    approval_token_secret: SecretStr = SecretStr(_DEV_SECRET)
    approval_token_ttl_seconds: int = 120
    approval_expiry_hours: int = 72
    credential_encryption_key: SecretStr | None = None

    # --- repositories ---
    repos_allowed_roots: str = ""
    max_file_bytes: int = 1_000_000
    index_max_files: int = 20_000

    # --- sandbox ---
    sandbox_mode: Literal["auto", "docker", "local"] = "auto"
    sandbox_image: str = "asea-sandbox:latest"
    sandbox_memory_mb: int = 1024
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = 256
    sandbox_default_timeout_seconds: int = 300
    # "container_prefix=host_prefix" when the backend itself runs in a container
    sandbox_path_map: str = ""

    # --- LLM ---
    llm_provider: Literal["openai", "gemini"] = "openai"
    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 2
    # 0 = no client-side pacing. Set just below the provider's requests-per-minute quota
    # (for example 12 on Gemini's free tier, which allows 15) to avoid running into it.
    llm_requests_per_minute: int = 0
    # How long one model call may wait in total for rate limits to clear before the run fails.
    llm_rate_limit_wait_seconds: float = 180.0

    # --- agent ---
    agent_max_tool_steps: int = 16
    agent_max_repair_attempts: int = 2
    agent_tool_result_chars: int = 12_000
    agent_context_chars: int = 120_000

    # --- MCP ---
    mcp_call_timeout_seconds: float = 60.0
    mcp_long_call_timeout_seconds: float = 900.0

    # --- GitHub ---
    github_api_url: str = "https://api.github.com"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_roots(self) -> list[Path]:
        return [Path(p.strip()).resolve() for p in self.repos_allowed_roots.split(",") if p.strip()]

    @property
    def llm_configured(self) -> bool:
        key = self.gemini_api_key if self.llm_provider == "gemini" else self.openai_api_key
        return bool(key and key.get_secret_value() and self.llm_model)

    @model_validator(mode="after")
    def _production_requires_real_secrets(self) -> Settings:
        if self.environment == "production":
            weak = [
                name
                for name in ("jwt_secret", "approval_token_secret")
                if getattr(self, name).get_secret_value() == _DEV_SECRET or len(getattr(self, name).get_secret_value()) < 32
            ]
            if not self.credential_encryption_key:
                weak.append("credential_encryption_key")
            if weak:
                raise ValueError(f"production requires strong values for: {', '.join(weak)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
