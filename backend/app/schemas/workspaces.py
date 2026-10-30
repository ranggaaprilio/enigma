from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.run_paths import RunPathError, validate_relative_subpath

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
    # Repo-relative subdirectory the workspace's commands and dev server run in.
    app_root: str | None = Field(default=None, max_length=512)
    # Repo-relative path (below app_root) where resolved env vars are materialised as a .env
    # file before each service launch. `None` clears it (no file written).
    env_file_path: str | None = Field(default=None, max_length=512)

    @field_validator("app_root")
    @classmethod
    def normalize_app_root(cls, value: str | None) -> str | None:
        try:
            return validate_relative_subpath(value)
        except RunPathError as error:
            raise ValueError(str(error)) from error

    @field_validator("env_file_path")
    @classmethod
    def normalize_env_file_path(cls, value: str | None) -> str | None:
        if value is not None and value.strip().endswith("/"):
            raise ValueError("env_file_path must not end with '/'")
        try:
            normalized = validate_relative_subpath(value)
        except RunPathError as error:
            raise ValueError(str(error)) from error
        if normalized is not None and normalized.split("/", 1)[0] == ".git":
            raise ValueError("env_file_path must not point inside .git")
        return normalized

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
    app_root: str | None = None
    env_file_path: str | None = None
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
    # Live, never stored: an actionable note from the provider (e.g. out-of-memory kills).
    status_detail: str | None = None
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
    # `None` => workspace-wide. Set to scope the variable to one service's launches only.
    custom_command_id: UUID | None = None

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
    scope: Literal["workspace", "service"] = "workspace"
    custom_command_id: UUID | None = None


class AffectedServiceResponse(BaseModel):
    service_id: UUID
    custom_command_id: UUID | None
    name: str
    status: str
    keys: list[str]


class WorkspaceEnvVarUpsertResponse(WorkspaceEnvVarResponse):
    affected_services: list[AffectedServiceResponse] = []
    warnings: list[str] = []


class WorkspaceEnvVarBulkItem(BaseModel):
    key: str
    status: Literal["created", "updated", "unchanged", "invalid"]
    message: str | None = None


class WorkspaceEnvVarBulkRequest(BaseModel):
    text: str | None = None
    values: dict[str, str] | None = None
    custom_command_id: UUID | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "WorkspaceEnvVarBulkRequest":
        if (self.text is None) == (self.values is None):
            raise ValueError("provide exactly one of text or values")
        return self


class WorkspaceEnvVarBulkResponse(BaseModel):
    items: list[WorkspaceEnvVarBulkItem]
    created: int
    updated: int
    unchanged: int
    invalid: int
    dry_run: bool
    affected_services: list[AffectedServiceResponse] = []
    warnings: list[str] = []


class WorkspaceEnvVarBulkDeleteRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=500)
    custom_command_id: UUID | None = None


class WorkspaceEnvVarBulkDeleteResponse(BaseModel):
    deleted: list[str]
    missing: list[str]
    affected_services: list[AffectedServiceResponse] = []


class DeleteResponse(BaseModel):
    ok: bool
