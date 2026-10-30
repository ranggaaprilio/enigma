"""Provider availability and the runtime choice at workspace creation (local-docker-runtime
Epic 06)."""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.services.sandbox.availability import (
    RuntimeAvailability,
    RuntimeChoiceError,
    check_runtime,
    choose_runtime,
)
from app.services.sandbox.base import RuntimeUnavailableError


class _Runtime:
    def __init__(self, *, configured: bool = True, health=None) -> None:
        self.configured = configured
        self._health = health

    async def health(self) -> str:
        if self._health is not None:
            return await self._health()
        return "ok"


async def _hangs() -> str:
    await asyncio.sleep(60)
    return "ok"


async def _fails() -> str:
    raise RuntimeUnavailableError("socket /var/run/docker.sock refused")


async def test_an_unconfigured_provider_is_never_probed() -> None:
    probed = False

    async def probe() -> str:
        nonlocal probed
        probed = True
        return "ok"

    result = await check_runtime(_Runtime(configured=False, health=probe), "docker")
    assert result == RuntimeAvailability(
        provider="docker",
        configured=False,
        status="unconfigured",
        reason="No Docker daemon is configured",
    )
    assert probed is False and result.available is False


async def test_a_healthy_provider_is_available() -> None:
    result = await check_runtime(_Runtime(), "daytona")
    assert (result.status, result.available, result.reason) == ("ok", True, None)


async def test_a_failing_or_hanging_probe_is_an_error_with_a_generic_reason() -> None:
    failed = await check_runtime(_Runtime(health=_fails), "docker")
    assert failed.status == "error"
    # Never the infrastructure detail (socket path, URL) from the underlying error.
    assert failed.reason == "The Docker daemon is not reachable"

    hung = await check_runtime(_Runtime(health=_hangs), "daytona", timeout=0.05)
    assert (hung.status, hung.reason) == ("error", "Daytona is not reachable")


def _availability(**statuses: str):
    async def availability(provider: str) -> RuntimeAvailability:
        status = statuses.get(provider, "unconfigured")
        return RuntimeAvailability(
            provider=provider,
            configured=status != "unconfigured",
            status=status,  # type: ignore[arg-type]
            reason=None if status == "ok" else f"{provider} {status}",
        )

    return availability


def _settings(**values) -> Settings:
    return Settings(
        **{"sandbox_runtime_default": "daytona", "metrics_token": "scrape-secret", **values}
    )


async def test_an_explicit_available_runtime_is_used() -> None:
    choose = _availability(daytona="ok", docker="ok")
    assert await choose_runtime("docker", _settings(), choose) == "docker"
    assert await choose_runtime("daytona", _settings(), choose) == "daytona"


async def test_an_explicit_unavailable_runtime_is_refused_not_swapped() -> None:
    with pytest.raises(RuntimeChoiceError) as raised:
        await choose_runtime("docker", _settings(), _availability(daytona="ok", docker="error"))
    assert raised.value.message == "Local Docker is not available: docker error"


async def test_no_choice_takes_the_default_when_it_is_available() -> None:
    choose = _availability(daytona="ok", docker="ok")
    assert await choose_runtime(None, _settings(), choose) == "daytona"
    assert (
        await choose_runtime(None, _settings(sandbox_runtime_default="docker"), choose)
        == "docker"
    )


async def test_no_choice_falls_back_to_the_other_provider_when_the_default_is_down() -> None:
    choose = _availability(docker="ok")  # no Daytona key: the self-host profile
    assert await choose_runtime(None, _settings(), choose) == "docker"


async def test_no_choice_with_neither_available_reports_the_default() -> None:
    with pytest.raises(RuntimeChoiceError) as raised:
        await choose_runtime(None, _settings(), _availability(docker="error"))
    assert raised.value.message == "Daytona is not available: daytona unconfigured"


async def test_docker_in_queue_mode_needs_the_single_host_flag() -> None:
    choose = _availability(daytona="ok", docker="ok")
    queue = _settings(run_execution_mode="queue")
    with pytest.raises(RuntimeChoiceError) as raised:
        await choose_runtime("docker", queue, choose)
    # Generic for the user; the operator finds the reason in the log.
    assert raised.value.message == "Local Docker is not available for this deployment"
    single_host = _settings(run_execution_mode="queue", docker_single_host=True)
    assert await choose_runtime("docker", single_host, choose) == "docker"
    # Daytona is unaffected by the flag.
    assert await choose_runtime("daytona", queue, choose) == "daytona"


async def test_the_fallback_to_docker_respects_the_queue_rule_too() -> None:
    with pytest.raises(RuntimeChoiceError):
        queue = _settings(run_execution_mode="queue")
        await choose_runtime(None, queue, _availability(docker="ok"))


async def test_docker_is_refused_without_a_metrics_token_even_when_not_the_default() -> None:
    """A Docker sandbox sits on a network the preview proxy is attached to, so with no token
    its tenant could read the proxy's /health and /metrics."""
    choose = _availability(daytona="ok", docker="ok")
    for token in (None, "", "   "):
        with pytest.raises(RuntimeChoiceError):
            await choose_runtime("docker", _settings(metrics_token=token), choose)
    assert await choose_runtime("daytona", _settings(metrics_token=None), choose) == "daytona"


def test_docker_as_default_requires_a_metrics_token() -> None:
    from app.services.sandbox.availability import require_metrics_token_for_docker

    for token in (None, "  "):
        with pytest.raises(RuntimeError, match="METRICS_TOKEN"):
            require_metrics_token_for_docker(
                _settings(sandbox_runtime_default="docker", metrics_token=token)
            )
    require_metrics_token_for_docker(
        _settings(sandbox_runtime_default="docker", metrics_token="s3cret")
    )
    require_metrics_token_for_docker(_settings(metrics_token=None))  # Daytona default


def test_an_unconfigured_default_with_a_configured_other_logs_a_warning(monkeypatch) -> None:
    from structlog.testing import capture_logs

    from app.services.sandbox import availability

    runtimes = {"daytona": _Runtime(configured=False), "docker": _Runtime()}
    monkeypatch.setattr(availability, "_runtime", runtimes.__getitem__)
    with capture_logs() as logs:
        availability.warn_if_default_runtime_unconfigured(_settings())
    assert [entry["event"] for entry in logs] == ["sandbox_runtime_default_unconfigured"]
    assert logs[0]["fallback"] == "docker"

    runtimes["daytona"] = _Runtime()
    with capture_logs() as logs:
        availability.warn_if_default_runtime_unconfigured(_settings())
    assert logs == []
