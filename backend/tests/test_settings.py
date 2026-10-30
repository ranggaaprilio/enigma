import pytest

from app.core.config import Settings


def test_settings_parse_comma_separated_cors_origins():
    settings = Settings(
        backend_cors_origins="http://localhost:5173, http://127.0.0.1:5173",
    )

    assert settings.backend_cors_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_command_allowlist_defaults_to_disabled_and_empty():
    settings = Settings()

    assert settings.agent_command_allowlist_enabled is False
    assert settings.agent_command_allowlist == []


def test_command_deny_dangerous_defaults_to_true():
    assert Settings().agent_command_deny_dangerous is True
    assert Settings(agent_command_deny_dangerous=False).agent_command_deny_dangerous is False


def test_command_allowlist_parses_comma_separated_string():
    settings = Settings(agent_command_allowlist="pytest, ls ,cat")

    assert settings.agent_command_allowlist == ["pytest", "ls", "cat"]


def test_sandbox_network_defaults_to_open_egress():
    settings = Settings()

    assert settings.sandbox_network_block_all is False
    assert settings.sandbox_network_allow_list == []


def test_sandbox_network_allow_list_parses_comma_separated_string():
    settings = Settings(sandbox_network_allow_list="10.0.0.0/16, 10.1.0.0/16 ")

    assert settings.sandbox_network_allow_list == ["10.0.0.0/16", "10.1.0.0/16"]


def test_egress_settings_accept_the_legacy_daytona_names(monkeypatch):
    """local-docker-runtime Epic 05: the egress policy is provider-neutral; the
    DAYTONA_NETWORK_* names keep working for existing deployments."""
    monkeypatch.delenv("SANDBOX_NETWORK_BLOCK_ALL", raising=False)
    monkeypatch.setenv("DAYTONA_NETWORK_BLOCK_ALL", "true")

    assert Settings().sandbox_network_block_all is True
    legacy = Settings(daytona_network_allow_list="10.0.0.0/8")
    assert legacy.sandbox_network_allow_list == ["10.0.0.0/8"]


def test_non_local_environment_requires_https_or_localhost_preview_proxy():
    """Epic 13: the preview-alias grant cookie needs SameSite=None; Secure, which cannot be set
    over plain HTTP except on the browser's localhost exemption."""
    with pytest.raises(ValueError, match="preview_proxy_base_url"):
        Settings(
            environment="production",
            auth_secret_key="a-real-secret-that-is-at-least-32-characters",
            workspace_env_encryption_key="q0pn5-5JpCUzSIqhCv4UbIi4_0wqQJ3vAfUZz1i2h9E=",
            metrics_token="a-real-token",
            daytona_preview_proxy_base_url="http://preview.example.com",
        )


def test_non_local_environment_allows_http_localhost_preview_proxy():
    settings = Settings(
        environment="production",
        auth_secret_key="a-real-secret-that-is-at-least-32-characters",
        workspace_env_encryption_key="q0pn5-5JpCUzSIqhCv4UbIi4_0wqQJ3vAfUZz1i2h9E=",
        metrics_token="a-real-token",
        preview_proxy_base_url="http://localhost:8001",
    )
    assert settings.preview_proxy_base_url == "http://localhost:8001"


def test_non_local_environment_allows_https_preview_proxy():
    settings = Settings(
        environment="production",
        auth_secret_key="a-real-secret-that-is-at-least-32-characters",
        workspace_env_encryption_key="q0pn5-5JpCUzSIqhCv4UbIi4_0wqQJ3vAfUZz1i2h9E=",
        metrics_token="a-real-token",
        daytona_preview_proxy_base_url="https://preview.example.com",
    )
    assert settings.preview_proxy_base_url == "https://preview.example.com"


def test_preview_proxy_settings_read_neutral_env_names(monkeypatch):
    """local-docker-runtime Epic 04: the proxy settings are provider-neutral."""
    monkeypatch.setenv("PREVIEW_PROXY_BASE_URL", "https://preview.example.com")
    monkeypatch.setenv("PREVIEW_PROXY_TTL_SECONDS", "120")

    settings = Settings()

    assert settings.preview_proxy_base_url == "https://preview.example.com"
    assert settings.preview_proxy_ttl_seconds == 120


def test_preview_proxy_settings_accept_legacy_daytona_env_names(monkeypatch):
    """Existing deployments set DAYTONA_PREVIEW_PROXY_*; they keep working until Epic 06."""
    monkeypatch.delenv("PREVIEW_PROXY_BASE_URL", raising=False)
    monkeypatch.delenv("PREVIEW_PROXY_TTL_SECONDS", raising=False)
    monkeypatch.setenv("DAYTONA_PREVIEW_PROXY_BASE_URL", "https://legacy.example.com")
    monkeypatch.setenv("DAYTONA_PREVIEW_PROXY_TTL_SECONDS", "90")

    settings = Settings()

    assert settings.preview_proxy_base_url == "https://legacy.example.com"
    assert settings.preview_proxy_ttl_seconds == 90


def test_neutral_preview_proxy_env_name_wins_over_the_legacy_one(monkeypatch):
    monkeypatch.setenv("PREVIEW_PROXY_BASE_URL", "https://new.example.com")
    monkeypatch.setenv("DAYTONA_PREVIEW_PROXY_BASE_URL", "https://legacy.example.com")

    assert Settings().preview_proxy_base_url == "https://new.example.com"


def test_preview_proxy_settings_accept_the_legacy_keyword():
    settings = Settings(daytona_preview_proxy_base_url="https://legacy.example.com")

    assert settings.preview_proxy_base_url == "https://legacy.example.com"
