from functools import lru_cache

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
    workspace_env_encryption_key: str = "SDJ5GYTawwnCZRYJ3wc8UNvbcmitbdkswaq14sV1phc="
    daytona_api_key: str | None = None
    daytona_api_url: str | None = None
    daytona_target: str | None = None
    daytona_default_snapshot: str | None = None
    daytona_default_language: str = "python"
    daytona_workspace_path: str = "workspace"
    daytona_sandbox_home: str = "/home/daytona"
    daytona_auto_stop_minutes: int = 30
    daytona_provision_timeout_seconds: int = 120
    daytona_preview_url_ttl_seconds: int = Field(default=60 * 60, gt=0)
    daytona_preview_proxy_base_url: str = "http://localhost:8001"
    daytona_preview_proxy_ttl_seconds: int = Field(default=60 * 60, gt=0)
    agent_provider: str = "auto"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    openhands_llm_base_url: str | None = None
    openhands_llm_api_key: str | None = None
    openhands_llm_model: str | None = None
    openhands_max_iterations: int = 30
    agent_command_timeout_seconds: int = 20
    agent_max_command_output_chars: int = 20_000
    terminal_default_cols: int = 120
    terminal_default_rows: int = 34
    editor_code_server_binary: str = "code-server"
    editor_port: int = 13337
    editor_token_ttl_seconds: int = 60 * 60
    editor_install_timeout_seconds: int = 180
    editor_auto_install: bool = True
    mise_auto_install: bool = True
    mise_install_url: str = "https://mise.run"
    dev_server_command_timeout_seconds: int = 30
    github_api_base_url: str = "https://api.github.com"
    github_api_timeout_seconds: int = 30
    max_worktrees_per_workspace: int = 5
    file_max_read_bytes: int = 1_000_000
    file_max_upload_bytes: int = 10_000_000
    file_tree_max_entries: int = 2_000
    file_tree_concurrency: int = 16
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
