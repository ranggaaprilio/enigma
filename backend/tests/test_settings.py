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


def test_daytona_network_defaults_to_open_egress():
    settings = Settings()

    assert settings.daytona_network_block_all is False
    assert settings.daytona_network_allow_list == []


def test_daytona_network_allow_list_parses_comma_separated_string():
    settings = Settings(daytona_network_allow_list="10.0.0.0/16, 10.1.0.0/16 ")

    assert settings.daytona_network_allow_list == ["10.0.0.0/16", "10.1.0.0/16"]


def test_non_local_environment_requires_https_or_localhost_preview_proxy():
    """Epic 13: the preview-alias grant cookie needs SameSite=None; Secure, which cannot be set
    over plain HTTP except on the browser's localhost exemption."""
    with pytest.raises(ValueError, match="daytona_preview_proxy_base_url"):
        Settings(
            environment="production",
            auth_secret_key="a-real-secret",
            workspace_env_encryption_key="a-real-key",
            metrics_token="a-real-token",
            daytona_preview_proxy_base_url="http://preview.example.com",
        )


def test_non_local_environment_allows_http_localhost_preview_proxy():
    settings = Settings(
        environment="production",
        auth_secret_key="a-real-secret",
        workspace_env_encryption_key="a-real-key",
        metrics_token="a-real-token",
        daytona_preview_proxy_base_url="http://localhost:8001",
    )
    assert settings.daytona_preview_proxy_base_url == "http://localhost:8001"


def test_non_local_environment_allows_https_preview_proxy():
    settings = Settings(
        environment="production",
        auth_secret_key="a-real-secret",
        workspace_env_encryption_key="a-real-key",
        metrics_token="a-real-token",
        daytona_preview_proxy_base_url="https://preview.example.com",
    )
    assert settings.daytona_preview_proxy_base_url == "https://preview.example.com"
