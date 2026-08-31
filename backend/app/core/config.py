from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Insecure defaults that must never be used outside a local environment. The fail-fast
# validator below refuses to boot when these are still in place and environment != "local".
_DEFAULT_AUTH_SECRET_KEY = "local-development-auth-secret-change-me"
_DEFAULT_ENV_ENCRYPTION_KEY = "SDJ5GYTawwnCZRYJ3wc8UNvbcmitbdkswaq14sV1phc="


class Settings(BaseSettings):
    app_name: str = "Execute Dashboard"
    environment: str = "local"
    database_url: str = "postgresql+asyncpg://execute:execute@localhost:5432/execute_dashboard"
    redis_url: str = "redis://localhost:6379/0"
    auth_secret_key: str = _DEFAULT_AUTH_SECRET_KEY
    auth_cookie_name: str = "execute_session"
    auth_session_ttl_seconds: int = 60 * 60 * 24 * 7
    auth_cookie_secure: bool = False
    auth_login_rate_limit_attempts: int = 5
    auth_login_rate_limit_window_seconds: int = 60 * 15
    agent_run_rate_limit_attempts: int = 30
    agent_run_rate_limit_window_seconds: int = 60
    workspace_env_encryption_key: str = _DEFAULT_ENV_ENCRYPTION_KEY
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
    daytona_network_block_all: bool = False
    daytona_network_allow_list: str | list[str] = Field(default_factory=list)
    agent_provider: str = "auto"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    openhands_llm_base_url: str | None = None
    openhands_llm_api_key: str | None = None
    openhands_llm_model: str | None = None
    openhands_max_iterations: int = 30
    agent_command_timeout_seconds: int = 20
    agent_max_command_output_chars: int = 20_000
    agent_command_allowlist_enabled: bool = False
    agent_command_allowlist: str | list[str] = Field(default_factory=list)
    # Multi-turn session context: total char budget for the prior-turn transcript prepended to
    # a session turn's prompt, and a per-turn cap on how much of a single turn's assistant text
    # is kept. ~4 chars/token, so 24k chars ≈ 6k tokens.
    agent_session_context_char_budget: int = 24_000
    agent_session_context_per_turn_char_cap: int = 8_000
    # Optional per-provider/model pricing overrides (USD per 1M tokens), merged over the
    # defaults in app.core.pricing. Accepts a JSON string via the MODEL_PRICING_OVERRIDES env
    # var, e.g. {"openai": {"gpt-4.1-mini": {"input_per_1m": 0.4, "output_per_1m": 1.6}}}.
    model_pricing_overrides: str | dict = Field(default_factory=dict)
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

    @field_validator("agent_command_allowlist", "daytona_network_allow_list", mode="before")
    @classmethod
    def parse_command_allowlist(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("model_pricing_overrides", mode="before")
    @classmethod
    def parse_model_pricing_overrides(cls, value: str | dict) -> dict:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return {}
            import json

            return json.loads(stripped)
        return value

    @model_validator(mode="after")
    def enforce_secure_defaults(self) -> "Settings":
        """Refuse to boot with insecure defaults outside a local environment.

        In any non-``local`` environment the app must be configured with real secrets and
        must serve cookies over HTTPS only. Constructing ``Settings`` (via ``get_settings``
        in ``create_app``) will raise here, so the process never starts insecurely.
        """
        if self.environment == "local":
            return self

        insecure = []
        if self.auth_secret_key == _DEFAULT_AUTH_SECRET_KEY:
            insecure.append("auth_secret_key")
        if self.workspace_env_encryption_key == _DEFAULT_ENV_ENCRYPTION_KEY:
            insecure.append("workspace_env_encryption_key")
        if insecure:
            raise ValueError(
                f"Insecure default secret(s) {insecure} are not allowed when "
                f"environment={self.environment!r}. Set real values via the environment "
                "before starting the app."
            )

        # Harden cookie handling in non-local environments regardless of the configured value.
        self.auth_cookie_secure = True
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
