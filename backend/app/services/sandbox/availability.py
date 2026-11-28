"""Which sandbox providers can take a new workspace (local-docker-runtime Epic 06; Docker removed
in Plan 8).

``check_runtime`` asks a provider, bounded by a short timeout: an unconfigured one is never
probed, and a failing or hanging probe is an ``error``. Reasons are generic on purpose --
they reach non-admin users (the create form, ``/health``), so never a socket path, URL or
version string from the underlying error.

``choose_runtime`` picks the provider at creation, before any row is written, so a provider
that is down fails the request with an actionable message instead of leaving a workspace
half-provisioned. An explicit choice is honoured or refused, never swapped; no choice takes
``SANDBOX_RUNTIME_DEFAULT``, or the other provider when only that one is available.

A provider left out of ``SANDBOX_RUNTIMES_ALLOWED`` is ``disabled``: never probed, never
chosen, and not a fault -- the operator turned it off for new workspaces.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import structlog

from app.core.config import Settings, get_settings
from app.services.sandbox.base import InsecureRuntimeError, SandboxRuntime

logger = structlog.get_logger(__name__)

PROVIDERS: tuple[str, ...] = ("daytona", "opensandbox")
HEALTH_TIMEOUT_SECONDS = 3.0

AvailabilityStatus = Literal["ok", "error", "unconfigured", "disabled", "insecure"]

PROVIDER_LABELS = {
    "daytona": "Daytona",
    "opensandbox": "OpenSandbox",
}
_REASONS = {
    ("daytona", "unconfigured"): "No Daytona API key is configured",
    ("daytona", "error"): "Daytona is not reachable",
    ("opensandbox", "unconfigured"): "No OpenSandbox server is configured",
    ("opensandbox", "error"): "The OpenSandbox server is not reachable",
    ("opensandbox", "insecure"): "The OpenSandbox server does not require authentication",
}
DISABLED_REASON = "Not enabled on this deployment"


@dataclass(frozen=True)
class RuntimeAvailability:
    provider: str
    configured: bool
    status: AvailabilityStatus
    reason: str | None

    @property
    def available(self) -> bool:
        return self.status == "ok"


class RuntimeChoiceError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def check_runtime(
    runtime: SandboxRuntime, provider: str, *, timeout: float = HEALTH_TIMEOUT_SECONDS
) -> RuntimeAvailability:
    if not runtime.configured:
        return RuntimeAvailability(
            provider, False, "unconfigured", _REASONS[(provider, "unconfigured")]
        )
    try:
        await asyncio.wait_for(runtime.health(), timeout)
    except InsecureRuntimeError:
        logger.warning("sandbox_runtime_insecure", provider=provider)
        return RuntimeAvailability(provider, True, "insecure", _REASONS[(provider, "insecure")])
    except Exception as error:  # TimeoutError included: a hung daemon is a down daemon
        logger.warning(
            "sandbox_runtime_unavailable", provider=provider, error_type=type(error).__name__
        )
        return RuntimeAvailability(provider, True, "error", _REASONS[(provider, "error")])
    return RuntimeAvailability(provider, True, "ok", None)


def _runtime(provider: str) -> SandboxRuntime:
    from app.services.sandbox.factory import get_runtime_by_name

    return get_runtime_by_name(provider)


async def runtime_availability(provider: str) -> RuntimeAvailability:
    runtime = _runtime(provider)
    if not get_settings().runtime_allowed(provider):
        return RuntimeAvailability(provider, runtime.configured, "disabled", DISABLED_REASON)
    return await check_runtime(runtime, provider)


def warn_if_default_runtime_unconfigured(settings: Settings) -> None:
    """At startup: say so when new workspaces will fall back to another provider."""
    default = settings.sandbox_runtime_default
    if _runtime(default).configured:
        return
    for other in (p for p in PROVIDERS if p != default):
        if settings.runtime_allowed(other) and _runtime(other).configured:
            logger.warning("sandbox_runtime_default_unconfigured", default=default, fallback=other)
            return


Availability = Callable[[str], Awaitable[RuntimeAvailability]]


def has_metrics_token(settings: Settings) -> bool:
    return bool(settings.metrics_token and settings.metrics_token.strip())


def _refusal(found: RuntimeAvailability) -> RuntimeChoiceError:
    return RuntimeChoiceError(
        f"{PROVIDER_LABELS[found.provider]} is not available: {found.reason}"
    )


async def choose_runtime(
    requested: str | None, settings: Settings, availability: Availability = runtime_availability
) -> str:
    if requested is not None:
        if not settings.runtime_allowed(requested):
            raise RuntimeChoiceError(
                f"{PROVIDER_LABELS[requested]} is not available: {DISABLED_REASON}"
            )
        found = await availability(requested)
        if not found.available:
            raise _refusal(found)
        return requested

    default = settings.sandbox_runtime_default
    found = await availability(default)
    if found.available:
        return default
    for other in (p for p in PROVIDERS if p != default):
        if not settings.runtime_allowed(other):
            continue
        fallback = await availability(other)
        if not fallback.available:
            continue
        logger.warning("sandbox_runtime_default_fallback", default=default, chosen=other)
        return other
    raise _refusal(found)
