from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Insecure defaults that must never be used outside a local environment. The fail-fast
# validator below refuses to boot when these are still in place and environment != "local".
_DEFAULT_AUTH_SECRET_KEY = "local-development-auth-secret-change-me"
_DEFAULT_ENV_ENCRYPTION_KEY = "SDJ5GYTawwnCZRYJ3wc8UNvbcmitbdkswaq14sV1phc="
# HMAC key length floor outside local: 32 chars ~ 256 bits of a random token.
_MIN_AUTH_SECRET_LENGTH = 32


class Settings(BaseSettings):
    app_name: str = "Execute Dashboard"
    environment: str = "local"
    database_url: str = "postgresql+asyncpg://execute:execute@localhost:5432/execute_dashboard"
    redis_url: str = "redis://localhost:6379/0"
    auth_secret_key: str = _DEFAULT_AUTH_SECRET_KEY
    auth_cookie_name: str = "execute_session"
    auth_session_ttl_seconds: int = 60 * 60 * 24 * 7
    auth_cookie_secure: bool = False
    # Bearer token required to scrape ``/metrics``. Unset leaves the endpoint open, which is
    # only allowed in a local environment (see ``enforce_secure_defaults``).
    metrics_token: str | None = None
    auth_login_rate_limit_attempts: int = 5
    auth_login_rate_limit_window_seconds: int = 60 * 15
    agent_run_rate_limit_attempts: int = 30
    agent_run_rate_limit_window_seconds: int = 60
    workspace_env_encryption_key: str = _DEFAULT_ENV_ENCRYPTION_KEY
    # Graceful shutdown (see app/core/lifecycle.py). After SIGTERM the instance keeps serving
    # with /ready=503 for the drain delay so the load balancer stops routing, then gives
    # in-flight runs the grace window before cancelling them. Keep the orchestrator's stop
    # timeout above the sum of both.
    shutdown_drain_delay_seconds: float = 5.0
    shutdown_run_grace_seconds: float = 15.0
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
    # The mock agent is a test/dev affordance, not a deployment mode. With nothing configured
    # (no platform_llm_settings row and no usable env config) an agent run is refused with a
    # 503 instead of silently replying with a canned transcript. Tests that exercise the run
    # pipeline without an LLM opt in explicitly.
    agent_allow_mock_provider: bool = False
    agent_command_timeout_seconds: int = 20
    agent_max_command_output_chars: int = 20_000
    agent_command_allowlist_enabled: bool = False
    agent_command_allowlist: str | list[str] = Field(default_factory=list)
    # When False, agent commands run unrestricted, including shell operators, absolute paths,
    # and programs in DANGEROUS_COMMANDS. Only use this for trusted agents in isolated sandboxes.
    agent_command_deny_dangerous: bool = True
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
    # TTL of the one-shot grant handed to the browser in the editor iframe's `src`. It only has
    # to survive the first navigation, after which the proxy's host-scoped cookie takes over --
    # so it is deliberately far shorter than `preview_alias_cookie_ttl_seconds`.
    editor_prime_ttl_seconds: int = Field(default=120, gt=0)
    editor_install_timeout_seconds: int = 180
    editor_auto_install: bool = True
    mise_auto_install: bool = True
    mise_install_url: str = "https://mise.run"
    dev_server_command_timeout_seconds: int = 30
    # User-authored registry commands get their own budget: `pnpm install` and
    # `alembic upgrade head` cannot finish inside agent_command_timeout_seconds (20s).
    command_run_timeout_seconds: int = 900
    command_run_max_timeout_seconds: int = 3600
    command_run_rate_limit_attempts: int = 30
    command_run_rate_limit_window_seconds: int = 60
    # Epic 11: multi-service preview. A sandbox is a shared, sized resource, so the number of
    # concurrently running services per workspace is capped.
    max_running_services_per_workspace: int = 4
    # Auto-resume never relaunches a "desired but down" service more often than this, so a
    # crash-looping process is not restarted every poll tick.
    service_relaunch_min_interval_seconds: int = 60
    # A service that has bound no port yet is reported "starting"; past this grace period since
    # launch it is reported "running" with an unknown port instead of "starting" forever.
    service_start_grace_seconds: int = 90
    # Upper bound on how long a single log-stream WebSocket keeps its sandbox `tail -F` alive;
    # the client reconnects (with replay) when it expires.
    service_log_stream_timeout_seconds: int = 3600
    # Epic 13: per-service HTTP health probe. With the 4-service cap this is at most ~6s serial
    # inside the existing dev_server_command_timeout_seconds exec budget; raising the cap would
    # need the probes threaded, not just this timeout raised.
    service_health_timeout_seconds: float = 1.5
    # Epic 13: preview aliases (stable per-service hostnames, opt-in per workspace).
    preview_alias_cookie_name: str = "enigma_preview_alias"
    preview_alias_cookie_ttl_seconds: int = Field(default=900, gt=0)
    preview_alias_grant_max_lifetime_seconds: int = Field(default=60 * 60 * 12, gt=0)
    preview_alias_capability_query_param: str = "__enigma_cap"
    # CSRF guard: for a non-safe method on an alias host, require Origin to be another host
    # under the proxy suffix belonging to the *same* workspace. SameSite=None is mandatory for
    # the alias grant cookie (it must ride along on a cross-site request), which otherwise
    # leaves the previewed app CSRF-reachable from anywhere.
    preview_alias_strict_origin: bool = True
    # Epic 13: CORS diagnosis storage (service_diagnostics), recorded by the preview proxy.
    service_diagnostic_throttle_seconds: int = Field(default=60, ge=0)
    service_diagnostic_retention_days: int = Field(default=7, gt=0)
    service_diagnostic_queue_maxsize: int = Field(default=256, gt=0)
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
        # Validation errors must not echo secrets into crash logs: pydantic would otherwise
        # append `input_value=` (the rejected key, or the whole settings dict for model-level
        # checks such as `enforce_secure_defaults`).
        hide_input_in_errors=True,
    )

    @field_validator("workspace_env_encryption_key")
    @classmethod
    def validate_fernet_key(cls, value: str) -> str:
        # Checked in every environment: a malformed key otherwise only fails on the first
        # encrypt/decrypt, long after boot.
        try:
            Fernet(value.encode("utf-8"))
        except (ValueError, TypeError) as error:
            raise ValueError(
                "workspace_env_encryption_key must be a valid Fernet key (32 url-safe "
                "base64-encoded bytes). Generate one with: python -c 'from cryptography.fernet "
                "import Fernet; print(Fernet.generate_key().decode())'"
            ) from error
        return value

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
        elif len(self.auth_secret_key) < _MIN_AUTH_SECRET_LENGTH:
            raise ValueError(
                f"auth_secret_key must be at least {_MIN_AUTH_SECRET_LENGTH} characters when "
                f"environment={self.environment!r}. Generate one with: "
                "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        if self.workspace_env_encryption_key == _DEFAULT_ENV_ENCRYPTION_KEY:
            insecure.append("workspace_env_encryption_key")
        # `/metrics` reports per-route request volumes and workspace-level counters; it must
        # not be world-readable outside local development.
        if not self.metrics_token:
            insecure.append("metrics_token")
        if insecure:
            raise ValueError(
                f"Insecure default secret(s) {insecure} are not allowed when "
                f"environment={self.environment!r}. Set real values via the environment "
                "before starting the app."
            )

        # Harden cookie handling in non-local environments regardless of the configured value.
        self.auth_cookie_secure = True

        # Epic 13: the preview-alias grant cookie must be `SameSite=None; Secure` (it has to
        # ride along on a cross-site request between two sibling preview hostnames), and
        # `SameSite=None` without `Secure` is rejected by every current browser. `Secure`
        # requires HTTPS, except for the browser's own `localhost` exemption -- so a plain
        # `http://` proxy base URL on any other host makes every alias grant unsettable.
        proxy_host = (urlsplit(self.daytona_preview_proxy_base_url).hostname or "").lower()
        proxy_is_loopback = proxy_host in {"localhost", "127.0.0.1", "::1"} or proxy_host.endswith(
            ".localhost"
        )
        if self.daytona_preview_proxy_base_url.startswith("http://") and not proxy_is_loopback:
            raise ValueError(
                "daytona_preview_proxy_base_url must be an https:// origin (or localhost) when "
                f"environment={self.environment!r} -- preview-alias grant cookies require "
                "SameSite=None; Secure, which cannot be set over plain HTTP on a non-loopback host."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
