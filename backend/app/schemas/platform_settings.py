from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

LlmConfigSource = Literal["database", "environment"]


class LlmSettingsStatusResponse(BaseModel):
    """Readable by any authenticated user, to drive the first-run gate.

    ``base_url`` and ``model`` are included so a non-owner's read-only view has something to
    show instead of an empty card. Neither is a credential; the API key never appears here.
    """

    configured: bool
    source: LlmConfigSource | None = None
    base_url: str | None = None
    model: str | None = None


class LlmSettingsResponse(BaseModel):
    """Owner-only. Never carries the API key -- only whether one exists, plus a constant mask."""

    base_url: str | None = None
    model: str | None = None
    has_api_key: bool = False
    masked_api_key: str | None = None
    source: LlmConfigSource | None = None
    updated_at: datetime | None = None


class LlmSettingsUpdateRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=2000)
    model: str = Field(min_length=1, max_length=150)
    # Tri-state, and the distinction matters: omitted/None keeps the stored key (the client
    # cannot read it back to resend it), "" clears it, a value replaces it.
    api_key: str | None = Field(default=None, max_length=500)


class LlmSettingsTestRequest(BaseModel):
    """Draft values so the owner can validate before saving. Omitted fields fall back to stored."""

    base_url: str | None = Field(default=None, max_length=2000)
    model: str | None = Field(default=None, max_length=150)
    api_key: str | None = Field(default=None, max_length=500)


class LlmProbeResponse(BaseModel):
    ok: bool
    message: str
    models: list[str] | None = None
    latency_ms: int | None = None


class LlmSettingsDeleteResponse(BaseModel):
    ok: bool
    cleared: bool
