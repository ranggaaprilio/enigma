from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

PreviewSessionStatus = Literal["active", "stopped", "error"]
PreviewAvailability = Literal["inactive", "active", "stopped", "error"]


class PreviewSessionResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    port: int
    public_url: str
    status: PreviewSessionStatus
    created_at: datetime
    last_seen_at: datetime
    # The worktree this preview belongs to (None = base clone) and its branch.
    worktree_id: UUID | None = None
    branch: str | None = None
    # Which workspace service this preview was detected for (Epic 11); None for a session
    # detected via the legacy single-port scan, before any service row claimed the port.
    service_id: UUID | None = None

    model_config = ConfigDict(from_attributes=True)


class PreviewDetectResponse(BaseModel):
    status: PreviewAvailability
    session: PreviewSessionResponse | None = None


# ---------------------------------------------------------------------------
# Epic 13: composed preview descriptor
# ---------------------------------------------------------------------------

PreviewHealth = Literal["unknown", "starting", "healthy", "unhealthy"]
ServiceDiagnosticKind = Literal["cors_missing_allow_origin", "cors_preflight_rejected"]
EntryPointSource = Literal["explicit", "inferred"]
AdvisorySeverity = Literal["info", "warning", "error"]


class ServiceDiagnosisResponse(BaseModel):
    id: UUID
    kind: ServiceDiagnosticKind
    origin: str
    request_method: str | None = None
    required_header: str | None = None
    request_path: str | None = None
    status_code: int | None = None
    detail: str | None = None
    occurrences: int
    first_seen_at: datetime
    last_seen_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PreviewCompositionServiceResponse(BaseModel):
    service_id: UUID
    custom_command_id: UUID | None = None
    name: str
    role: str
    kind: str
    port: int | None = None
    status: str
    health_status: PreviewHealth = "unknown"
    health_checked_at: datetime | None = None
    is_entry_point: bool
    entry_point_source: EntryPointSource
    branch: str | None = None
    worktree_id: UUID | None = None
    preview_session_id: UUID | None = None
    # Rotates hourly by default -- the frontend labels it "temporary link".
    capability_url: str | None = None
    capability_url_rotates: bool = True
    # Stable -- safe to paste into an environment variable. Both None when aliases are
    # disabled for the workspace, or none has been minted yet for this row.
    alias: str | None = None
    alias_url: str | None = None
    alias_prime_url: str | None = None
    localhost_url: str | None = None
    diagnoses: list[ServiceDiagnosisResponse] = []

    model_config = ConfigDict(from_attributes=True)


class PreviewAdvisoryResponse(BaseModel):
    code: str
    severity: AdvisorySeverity
    title: str
    detail: str
    framework: str | None = None
    snippet: str | None = None
    snippet_language: str | None = None
    target_file: str | None = None
    service_ids: list[UUID] = []

    model_config = ConfigDict(from_attributes=True)


class PreviewCompositionResponse(BaseModel):
    workspace_id: UUID
    preview_alias_enabled: bool
    proxy_base_url: str
    entry_point_service_id: UUID | None = None
    services: list[PreviewCompositionServiceResponse]
    advisories: list[PreviewAdvisoryResponse] = []

    model_config = ConfigDict(from_attributes=True)


class PreviewAliasToggleResponse(BaseModel):
    preview_alias_enabled: bool


class PreviewAliasRegenerateRequest(BaseModel):
    custom_command_id: UUID | None = None


class PreviewAliasRegenerateResponse(BaseModel):
    alias: str
    alias_url: str
