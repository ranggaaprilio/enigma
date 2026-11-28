"""Plan 8: the custom Docker provider is gone (last release with it: v0.1.0).

OpenSandbox replaced it for self-hosting (Plan 7b). These pin what an upgrade sees: Docker is no
longer a provider anyone can choose, a stale configuration says which release to stay on, and
the leftover Docker settings are reported rather than fatal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, deprecated_setting_names, removed_setting_names
from app.schemas.workspaces import WorkspaceCreateRequest
from app.services.sandbox import get_runtime_by_name
from app.services.sandbox.availability import PROVIDER_LABELS, PROVIDERS
from app.services.sandbox.base import RuntimeUnavailableError

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_only_daytona_and_opensandbox_are_providers() -> None:
    assert PROVIDERS == ("daytona", "opensandbox")
    assert set(PROVIDER_LABELS) == {"daytona", "opensandbox"}


@pytest.mark.parametrize(
    "values",
    [{"sandbox_runtime_default": "docker"}, {"sandbox_runtimes_allowed": "docker"}],
)
def test_a_docker_configuration_names_the_last_release_with_it(values) -> None:
    with pytest.raises(ValidationError, match=r"v0\.1\.0"):
        Settings(**values)


def test_a_new_workspace_cannot_ask_for_docker() -> None:
    with pytest.raises(ValidationError):
        WorkspaceCreateRequest(name="Docker", source="blank", runtime="docker")


def test_a_leftover_docker_workspace_has_no_provider() -> None:
    with pytest.raises(RuntimeUnavailableError):
        get_runtime_by_name("docker")


def test_stale_docker_settings_are_reported_not_fatal() -> None:
    environ = {"DOCKER_HOST": "unix:///x", "DOCKER_UPKEEP": "true", "PATH": "/bin"}
    assert removed_setting_names(environ, env_file=None) == ["DOCKER_HOST", "DOCKER_UPKEEP"]
    # `extra="ignore"`: they never stop a start.
    Settings(docker_sandbox_image="enigma-sandbox:latest", docker_upkeep=True)


def test_the_upkeep_interval_has_a_neutral_name_and_keeps_the_old_one() -> None:
    assert Settings(sandbox_upkeep_interval_seconds=5).sandbox_upkeep_interval_seconds == 5
    assert Settings(docker_reaper_interval_seconds=7).sandbox_upkeep_interval_seconds == 7
    assert deprecated_setting_names({"DOCKER_REAPER_INTERVAL_SECONDS": "7"}, env_file=None) == {
        "DOCKER_REAPER_INTERVAL_SECONDS": "SANDBOX_UPKEEP_INTERVAL_SECONDS"
    }
    # The alias is not reported as removed: it still works for one release.
    assert removed_setting_names({"DOCKER_REAPER_INTERVAL_SECONDS": "7"}, env_file=None) == []


def test_nothing_in_the_app_imports_the_docker_sdk() -> None:
    sdk = re.compile(r"^\s*(from|import)\s+docker\b", re.MULTILINE)
    offenders = [p for p in APP_DIR.rglob("*.py") if sdk.search(p.read_text())]
    assert offenders == []
