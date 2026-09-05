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
