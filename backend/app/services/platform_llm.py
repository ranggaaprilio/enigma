"""Platform-wide LLM configuration: storage, resolution, and endpoint probing.

Configuration lives in the ``platform_llm_settings`` singleton row, set by the platform owner
through the Global Settings page. The environment variables (``OPENHANDS_LLM_*``) remain a
fallback so existing deployments keep working without being reconfigured through the UI.

Resolution precedence is database, then environment, then unconfigured -- see ``resolve()``.
"""

import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import PlatformLlmSettings
from app.services.workspaces import WorkspaceSecretCipher

logger = structlog.get_logger("app.platform_llm")

LlmConfigSource = Literal["database", "environment"]

# The probe is a plain GET against an owner-supplied URL, so it is bounded rather than
# unrestricted: only http/https, no redirect following, a hard timeout, and a capped read.
PROBE_TIMEOUT_SECONDS = 10.0
PROBE_MAX_RESPONSE_BYTES = 256_000
_ALLOWED_SCHEMES = {"http", "https"}
# Loopback and RFC1918 are deliberately NOT blocked: a local Ollama/LM Studio endpoint is the
# primary use case, so a private-range denylist would break the feature it exists to serve.
# The cloud metadata addresses are the one exception -- nothing legitimate serves an LLM there.
_BLOCKED_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.goog",
    "fd00:ec2::254",
}


class InvalidLlmSettingsError(ValueError):
    """The supplied LLM settings are not usable."""


class InvalidLlmBaseUrlError(InvalidLlmSettingsError):
    """The supplied base URL is not a usable http(s) URL."""


class InvalidLlmModelError(InvalidLlmSettingsError):
    """The supplied model name is empty."""


class LlmSettingsDecryptionError(Exception):
    """The stored API key could not be decrypted -- the encryption key changed."""


@dataclass(frozen=True)
class ResolvedLlmConfig:
    base_url: str
    model: str
    api_key: str | None
    source: LlmConfigSource


@dataclass(frozen=True)
class LlmProbeResult:
    ok: bool
    message: str
    models: list[str] | None
    latency_ms: int | None


def validate_base_url(value: str) -> str:
    """Normalise and validate an OpenAI-compatible base URL.

    Trailing slashes are stripped so ``{base_url}/models`` never double-slashes.
    """
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        raise InvalidLlmBaseUrlError("Base URL is required.")
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise InvalidLlmBaseUrlError("Base URL must start with http:// or https://.")
    if not parts.hostname:
        raise InvalidLlmBaseUrlError("Base URL must include a host.")
    if parts.hostname.lower() in _BLOCKED_HOSTS:
        raise InvalidLlmBaseUrlError("That host is not allowed.")
    return candidate


class PlatformLlmSettingsService:
    def __init__(
        self,
        session: AsyncSession,
        cipher: WorkspaceSecretCipher,
        settings: Settings,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.settings = settings

    async def get_row(self) -> PlatformLlmSettings | None:
        result = await self.session.execute(select(PlatformLlmSettings).limit(1))
        return result.scalar_one_or_none()

    def _decrypt_key(self, row: PlatformLlmSettings) -> str | None:
        if not row.encrypted_api_key:
            return None
        plaintext = self.cipher.decrypt(row.encrypted_api_key)
        if plaintext is None:
            # WORKSPACE_ENV_ENCRYPTION_KEY was rotated, or the row is corrupt. Raising beats
            # returning None: a None key reaches OpenHandsAgentProvider, which then sends
            # `Authorization: ""` and produces an opaque 401 from the endpoint. An explicit
            # error tells the owner exactly what to do -- re-enter the key.
            logger.warning("platform_llm.api_key_undecryptable")
            raise LlmSettingsDecryptionError(
                "The stored LLM API key cannot be decrypted -- the encryption key changed. "
                "Re-enter the API key in Settings."
            )
        return plaintext

    async def resolve(self) -> ResolvedLlmConfig | None:
        """The effective LLM configuration, or None when nothing is configured."""
        row = await self.get_row()
        if row is not None:
            return ResolvedLlmConfig(
                base_url=row.base_url,
                model=row.model,
                api_key=self._decrypt_key(row),
                source="database",
            )

        env_base_url = (self.settings.openhands_llm_base_url or "").strip()
        env_model = (self.settings.openhands_llm_model or "").strip()
        if env_base_url and env_model:
            return ResolvedLlmConfig(
                base_url=env_base_url,
                model=env_model,
                api_key=(self.settings.openhands_llm_api_key or "").strip() or None,
                source="environment",
            )
        return None

    async def upsert(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        user_id: UUID,
    ) -> PlatformLlmSettings:
        """Create or replace the singleton row.

        ``api_key`` semantics, which the API contract depends on:

        * ``None``  -- keep whatever key is already stored. The client cannot read the key back,
          so "I did not touch the key field" must not wipe it.
        * ``""``    -- clear the stored key, for switching to a keyless local endpoint.
        * non-empty -- encrypt and replace.
        """
        normalized_url = validate_base_url(base_url)
        normalized_model = (model or "").strip()
        if not normalized_model:
            raise InvalidLlmModelError("Model is required.")

        row = await self.get_row()
        if row is None:
            row = PlatformLlmSettings(
                base_url=normalized_url,
                model=normalized_model,
                encrypted_api_key=(self.cipher.encrypt(api_key) if api_key else None),
                updated_by_user_id=user_id,
            )
            self.session.add(row)
        else:
            row.base_url = normalized_url
            row.model = normalized_model
            row.updated_by_user_id = user_id
            if api_key is not None:
                row.encrypted_api_key = self.cipher.encrypt(api_key) if api_key else None

        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def clear(self) -> bool:
        """Delete the singleton row so resolution falls back to the environment."""
        row = await self.get_row()
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.commit()
        return True

    async def probe(
        self,
        base_url: str,
        api_key: str | None,
        model: str | None = None,
    ) -> LlmProbeResult:
        """Verify that a base URL speaks the OpenAI-compatible API.

        ``GET {base_url}/models`` first: it is cheap, needs no token budget, and its
        ``data[].id`` list populates the model dropdown. Gateways that do not implement it
        (404/405) fall back to a 1-token ``POST {base_url}/chat/completions``, the one call
        every OpenAI-compatible endpoint must support -- so a missing ``/models`` is reported
        as a real pass rather than an unverified guess.
        """
        normalized_url = validate_base_url(base_url)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        started = time.monotonic()

        try:
            async with httpx.AsyncClient(
                timeout=PROBE_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                response = await client.get(f"{normalized_url}/models", headers=headers)
                if response.status_code in (404, 405) and model:
                    return await self._probe_completion(
                        client, normalized_url, headers, model, started
                    )
        except httpx.ConnectTimeout:
            return LlmProbeResult(False, "The endpoint did not respond within 10s.", None, None)
        except httpx.ReadTimeout:
            return LlmProbeResult(False, "The endpoint did not respond within 10s.", None, None)
        except httpx.ConnectError as error:
            if "certificate" in str(error).lower() or "ssl" in str(error).lower():
                return LlmProbeResult(
                    False,
                    "TLS handshake failed. Check the URL's scheme and certificate.",
                    None,
                    None,
                )
            return LlmProbeResult(
                False,
                "Could not reach the endpoint. Check the base URL and that the server is running.",
                None,
                None,
            )
        except httpx.HTTPError as error:
            logger.warning("platform_llm.probe_failed", error=str(error))
            return LlmProbeResult(False, "The connection test failed.", None, None)

        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code in (401, 403):
            return LlmProbeResult(False, "The endpoint rejected the API key.", None, latency_ms)
        if response.status_code in (404, 405):
            # Only reached when no model was supplied to fall back with. Most often the base
            # URL is missing the provider's /v1 suffix.
            return LlmProbeResult(
                False,
                f"No OpenAI-compatible API found at {normalized_url} "
                f"(HTTP {response.status_code}). Most endpoints need a /v1 suffix.",
                None,
                latency_ms,
            )
        if response.status_code >= 400:
            return LlmProbeResult(
                False,
                f"The endpoint returned HTTP {response.status_code}.",
                None,
                latency_ms,
            )

        models = _parse_model_ids(response)
        if models is None:
            return LlmProbeResult(
                True,
                "Connected, but the model list could not be parsed.",
                None,
                latency_ms,
            )
        return LlmProbeResult(
            True,
            f"Connected. {len(models)} model(s) available.",
            models,
            latency_ms,
        )

    async def _probe_completion(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
        model: str,
        started: float,
    ) -> LlmProbeResult:
        """Fallback for endpoints without /models: ask for one token and see if it answers."""
        try:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "stream": False,
                },
            )
        except httpx.HTTPError:
            return LlmProbeResult(
                False,
                "The endpoint has no /models list and did not answer a test completion.",
                None,
                None,
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code in (401, 403):
            return LlmProbeResult(False, "The endpoint rejected the API key.", None, latency_ms)
        if response.status_code >= 400:
            return LlmProbeResult(
                False,
                f'Connected, but model "{model}" was rejected (HTTP {response.status_code}).',
                None,
                latency_ms,
            )
        return LlmProbeResult(
            True,
            f'Connected. Model "{model}" answered.',
            None,
            latency_ms,
        )


def _parse_model_ids(response: httpx.Response) -> list[str] | None:
    if len(response.content) > PROBE_MAX_RESPONSE_BYTES:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None
    ids = [
        str(entry["id"])
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]
    return sorted(ids)
