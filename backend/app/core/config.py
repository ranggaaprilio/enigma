from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Execute Dashboard"
    environment: str = "local"
    database_url: str = "postgresql+asyncpg://execute:execute@localhost:5432/execute_dashboard"
    redis_url: str = "redis://localhost:6379/0"
    auth_secret_key: str = "local-development-auth-secret-change-me"
    auth_cookie_name: str = "execute_session"
    auth_session_ttl_seconds: int = 60 * 60 * 24 * 7
    auth_cookie_secure: bool = False
    auth_login_rate_limit_attempts: int = 5
    auth_login_rate_limit_window_seconds: int = 60 * 15
    workspace_root: Path = Path(".data/workspaces")
    workspace_env_encryption_key: str = "SDJ5GYTawwnCZRYJ3wc8UNvbcmitbdkswaq14sV1phc="
    backend_cors_origins: str | list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("backend_cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
