from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

WorkspaceStatus = Literal["creating", "ready", "busy", "error", "stopped"]
# The requesting user's role *in this workspace* (see app.services.access.WorkspaceRole).
WorkspaceViewerRole = Literal["owner", "team_lead", "team_member", "team_viewer"]


def normalize_nonempty(value: str) -> str:
    return value.strip()


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    repo_url: str = Field(min_length=1, max_length=2048)
    default_branch: str = Field(default="main", min_length=1, max_length=120)
    snapshot: str | None = Field(default=None, max_length=255)

    @field_validator("name", "repo_url", "default_branch")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        stripped = normalize_nonempty(value)
        if not stripped:
            raise ValueError("Value cannot be empty")
        return stripped

    @field_validator("snapshot")
    @classmethod
    def normalize_snapshot(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class WorkspaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    repo_url: str | None = Field(default=None, min_length=1, max_length=2048)
    default_branch: str | None = Field(default=None, min_length=1, max_length=120)
    snapshot: str | None = Field(default=None, max_length=255)

    @field_validator("name", "repo_url", "default_branch")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = normalize_nonempty(value)
        if not stripped:
            raise ValueError("Value cannot be empty")
        return stripped

    @field_validator("snapshot")
    @classmethod
    def normalize_optional_snapshot(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class WorkspaceResponse(BaseModel):
    id: UUID
    owner_id: UUID
    name: str
    slug: str
    repo_url: str
    default_branch: str
    runtime: str
    sandbox_id: str | None
    snapshot: str | None
    workspace_path: str
    status: WorkspaceStatus
    created_at: datetime
    updated_at: datetime
    # Role of the user who requested this workspace; `None` when the caller's role could not
    # be resolved. Clients use it to hide/disable actions they cannot perform.
    viewer_role: WorkspaceViewerRole | None = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("runtime", mode="before")
    @classmethod
    def default_runtime(cls, value: str | None) -> str:
        return value or "daytona"

    @field_validator("workspace_path", mode="before")
    @classmethod
    def default_workspace_path(cls, value: str | None) -> str:
        return value or "workspace"


class WorkspaceStatusResponse(BaseModel):
    id: UUID
    status: WorkspaceStatus
    runtime: str
    sandbox_id: str | None
    snapshot: str | None
    workspace_path: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("runtime", mode="before")
    @classmethod
    def default_status_runtime(cls, value: str | None) -> str:
        return value or "daytona"

    @field_validator("workspace_path", mode="before")
    @classmethod
    def default_status_workspace_path(cls, value: str | None) -> str:
        return value or "workspace"


class WorkspaceEnvVarUpsertRequest(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=20_000)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return value.strip()


class WorkspaceEnvVarResponse(BaseModel):
    id: UUID
    key: str
    masked_value: str
    created_at: datetime
    updated_at: datetime


class DeleteResponse(BaseModel):
    ok: bool
