from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WorkspaceStatus = Literal["creating", "ready", "busy", "error", "stopped"]
# Where the workspace's repository comes from. ``repository`` clones an existing remote (needs
# ``repo_url``), ``template`` scaffolds from a ``workspace_templates`` row (needs ``template_id``),
# ``blank`` starts with an empty ``git init``. Template/blank workspaces have no ``repo_url`` until
# a remote is connected.
WorkspaceSource = Literal["repository", "template", "blank"]
# The requesting user's role *in this workspace* (see app.services.access.WorkspaceRole).
WorkspaceViewerRole = Literal["owner", "team_lead", "team_member", "team_viewer"]


def normalize_nonempty(value: str) -> str:
    return value.strip()


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    source: WorkspaceSource = "repository"
    repo_url: str | None = Field(default=None, max_length=2048)
    template_id: UUID | None = None
    default_branch: str = Field(default="main", min_length=1, max_length=120)
    snapshot: str | None = Field(default=None, max_length=255)

    @field_validator("name", "default_branch")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        stripped = normalize_nonempty(value)
        if not stripped:
            raise ValueError("Value cannot be empty")
        return stripped

    @field_validator("repo_url", "snapshot")
    @classmethod
    def normalize_snapshot(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def check_source_fields(self) -> "WorkspaceCreateRequest":
        if self.source == "repository":
            if not self.repo_url:
                raise ValueError("repo_url is required when source is 'repository'")
            if self.template_id is not None:
                raise ValueError("template_id must be omitted when source is 'repository'")
        elif self.source == "template":
            if self.template_id is None:
                raise ValueError("template_id is required when source is 'template'")
            if self.repo_url:
                raise ValueError("repo_url must be omitted when source is 'template'")
        else:
            if self.repo_url or self.template_id is not None:
                raise ValueError("repo_url and template_id must be omitted for a blank workspace")
        return self


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
    repo_url: str | None
    source: WorkspaceSource = "repository"
    template_id: UUID | None = None
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

    @field_validator("source", mode="before")
    @classmethod
    def default_source(cls, value: str | None) -> str:
        return value or "repository"

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
