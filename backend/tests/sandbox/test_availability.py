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

    result = await check_runtime(_Runtime(configured=False, health=probe), "opensandbox")
    assert result == RuntimeAvailability(
        provider="opensandbox",
        configured=False,
        status="unconfigured",
        reason="No OpenSandbox server is configured",
    )
    assert probed is False and result.available is False


async def test_a_healthy_provider_is_available() -> None:
    result = await check_runtime(_Runtime(), "daytona")
    assert (result.status, result.available, result.reason) == ("ok", True, None)


async def test_a_failing_or_hanging_probe_is_an_error_with_a_generic_reason() -> None:
    failed = await check_runtime(_Runtime(health=_fails), "opensandbox")
    assert failed.status == "error"
    # Never the infrastructure detail (socket path, URL) from the underlying error.
    assert failed.reason == "The OpenSandbox server is not reachable"

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
    return Settings(**{"sandbox_runtime_default": "daytona", **values})


async def test_an_explicit_available_runtime_is_used() -> None:
    choose = _availability(daytona="ok", opensandbox="ok")
    assert await choose_runtime("daytona", _settings(), choose) == "daytona"
    assert await choose_runtime("opensandbox", _settings(), choose) == "opensandbox"


async def test_an_explicit_unavailable_runtime_is_refused_not_swapped() -> None:
    with pytest.raises(RuntimeChoiceError) as raised:
        await choose_runtime(
            "opensandbox", _settings(), _availability(daytona="ok", opensandbox="error")
        )
    assert raised.value.message == "OpenSandbox is not available: opensandbox error"


async def test_no_choice_takes_the_default_when_it_is_available() -> None:
    choose = _availability(daytona="ok", opensandbox="ok")
    assert await choose_runtime(None, _settings(), choose) == "daytona"
    assert (
        await choose_runtime(None, _settings(sandbox_runtime_default="opensandbox"), choose)
        == "opensandbox"
    )


async def test_no_choice_falls_back_to_the_other_provider_when_the_default_is_down() -> None:
    choose = _availability(opensandbox="ok")  # no Daytona key: the self-host profile
    assert await choose_runtime(None, _settings(), choose) == "opensandbox"


async def test_no_choice_with_neither_available_reports_the_default() -> None:
    with pytest.raises(RuntimeChoiceError) as raised:
        await choose_runtime(None, _settings(), _availability(opensandbox="error"))
    assert raised.value.message == "Daytona is not available: daytona unconfigured"


def test_an_unconfigured_default_with_a_configured_other_logs_a_warning(monkeypatch) -> None:
    from structlog.testing import capture_logs

    from app.services.sandbox import availability

    runtimes = {"daytona": _Runtime(configured=False), "opensandbox": _Runtime()}
    monkeypatch.setattr(availability, "_runtime", runtimes.__getitem__)
    with capture_logs() as logs:
        availability.warn_if_default_runtime_unconfigured(_settings())
    assert [entry["event"] for entry in logs] == ["sandbox_runtime_default_unconfigured"]
    assert logs[0]["fallback"] == "opensandbox"

    runtimes["daytona"] = _Runtime()
    with capture_logs() as logs:
        availability.warn_if_default_runtime_unconfigured(_settings())
    assert logs == []


async def test_a_provider_left_out_of_the_allow_list_is_refused_when_picked() -> None:
    choose = _availability(daytona="ok", opensandbox="ok")
    with pytest.raises(RuntimeChoiceError) as raised:
        await choose_runtime("opensandbox", _settings(sandbox_runtimes_allowed="daytona"), choose)
    assert raised.value.message == "OpenSandbox is not available: Not enabled on this deployment"


async def test_no_choice_never_falls_back_to_a_disallowed_provider() -> None:
    allowed = _settings(sandbox_runtimes_allowed="daytona")
    with pytest.raises(RuntimeChoiceError) as raised:
        await choose_runtime(None, allowed, _availability(opensandbox="ok"))
    assert raised.value.message == "Daytona is not available: daytona unconfigured"


def test_the_allow_list_must_name_known_providers_and_include_the_default() -> None:
    assert _settings(
        sandbox_runtimes_allowed="daytona, opensandbox"
    ).sandbox_runtimes_allowed == ["daytona", "opensandbox"]
    with pytest.raises(ValueError, match="unknown providers"):
        _settings(sandbox_runtimes_allowed="daytona,k8s")
    with pytest.raises(ValueError, match="must include sandbox_runtime_default"):
        _settings(sandbox_runtimes_allowed="opensandbox")


async def test_runtime_availability_reports_a_disallowed_provider_as_disabled(
    monkeypatch,
) -> None:
    from app.services.sandbox import availability

    probed = False

    async def probe() -> str:
        nonlocal probed
        probed = True
        return "ok"

    monkeypatch.setattr(availability, "_runtime", lambda _: _Runtime(health=probe))
    monkeypatch.setattr(
        availability, "get_settings", lambda: _settings(sandbox_runtimes_allowed="daytona")
    )

    found = await availability.runtime_availability("opensandbox")

    assert (found.status, found.available, found.reason) == (
        "disabled",
        False,
        "Not enabled on this deployment",
    )
    assert probed is False


def test_the_startup_warning_ignores_a_disallowed_fallback(monkeypatch) -> None:
    from structlog.testing import capture_logs

    from app.services.sandbox import availability

    runtimes = {"daytona": _Runtime(configured=False), "opensandbox": _Runtime()}
    monkeypatch.setattr(availability, "_runtime", runtimes.__getitem__)
    with capture_logs() as logs:
        availability.warn_if_default_runtime_unconfigured(
            _settings(sandbox_runtimes_allowed="daytona")
        )
    assert logs == []
