from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.run_paths import RunPathError, validate_relative_subpath

CommandRunStatus = Literal["running", "completed", "failed", "error"]
CommandRole = Literal["install", "migrate", "dev", "test", "lint", "seed", "custom"]
# "service" is accepted by the schema but only executed from Epic 11 onwards.
CommandKind = Literal["task", "service"]


def normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def normalize_subpath(value: str | None) -> str | None:
    """Reject absolute paths and ``..`` before anything reaches the database."""
    try:
        return validate_relative_subpath(value)
    except RunPathError as error:
        raise ValueError(str(error)) from error


def normalize_health_path(value: str | None) -> str | None:
    """Mirrors ``ck_custom_commands_health_path``: a root-relative HTTP path, like a URL path."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if not stripped.startswith("/"):
        raise ValueError("health_path must start with '/'")
    return stripped


class CustomCommandCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    command: str = Field(min_length=1, max_length=4000)
    description: str | None = Field(default=None, max_length=4000)
    icon: str | None = Field(default=None, max_length=80)
    hotkey: str | None = Field(default=None, max_length=80)
    requires_confirmation: bool = False
    role: CommandRole = "custom"
    kind: CommandKind = "task"
    cwd: str | None = Field(default=None, max_length=512)
    port: int | None = Field(default=None, ge=1, le=65535)
    enabled: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    # Epic 13: readiness + "Open app" entry point. Only meaningful for a `kind="service"`
    # command -- enforced by the service layer (which knows the *effective* kind after role
    # defaulting) and, as a last line of defense, the database CHECK.
    health_path: str | None = Field(default=None, max_length=512)
    is_entry_point: bool = False

    @field_validator("cwd")
    @classmethod
    def normalize_cwd(cls, value: str | None) -> str | None:
        return normalize_subpath(value)

    @field_validator("health_path")
    @classmethod
    def normalize_health_path_field(cls, value: str | None) -> str | None:
        return normalize_health_path(value)

    @field_validator("name", "command")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value cannot be empty")
        return stripped

    @field_validator("description", "icon", "hotkey")
    @classmethod
    def normalize_optional_fields(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)


# Columns that are NOT NULL on CustomCommand: an update may omit these (leaving the stored
# value alone) but must not explicitly null them out. Every field here is typed ``X | None``
# only so it can be left unset — see ``_reject_explicit_null_on_non_nullable_fields``.
_NON_NULLABLE_UPDATE_FIELDS = (
    "name",
    "command",
    "requires_confirmation",
    "role",
    "kind",
    "enabled",
    "is_entry_point",
)


class CustomCommandUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    command: str | None = Field(default=None, min_length=1, max_length=4000)
    description: str | None = Field(default=None, max_length=4000)
    icon: str | None = Field(default=None, max_length=80)
    hotkey: str | None = Field(default=None, max_length=80)
    requires_confirmation: bool | None = None
    role: CommandRole | None = None
    kind: CommandKind | None = None
    cwd: str | None = Field(default=None, max_length=512)
    port: int | None = Field(default=None, ge=1, le=65535)
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    health_path: str | None = Field(default=None, max_length=512)
    is_entry_point: bool | None = None

    @field_validator("cwd")
    @classmethod
    def normalize_cwd(cls, value: str | None) -> str | None:
        return normalize_subpath(value)

    @field_validator("health_path")
    @classmethod
    def normalize_health_path_field(cls, value: str | None) -> str | None:
        return normalize_health_path(value)

    @field_validator("name", "command")
    @classmethod
    def normalize_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value cannot be empty")
        return stripped

    @field_validator("description", "icon", "hotkey")
    @classmethod
    def normalize_optional_fields(cls, value: str | None) -> str | None:
        return normalize_optional_text(value)

    @model_validator(mode="after")
    def _reject_explicit_null_on_non_nullable_fields(self) -> "CustomCommandUpdateRequest":
        """``exclude_unset`` keeps a field the client sent as ``"field": null`` (it *was* set,
        just to ``None``), which the service then assigns straight onto the ORM row. For a NOT
        NULL/CHECK-constrained column that raised an unhandled IntegrityError on commit — a 500,
        not a 422 — for e.g. ``{"role": null}``. These fields are ``X | None`` only so a PATCH can
        omit them; an explicit null is a client error caught here instead.
        """
        offending = [
            field
            for field in _NON_NULLABLE_UPDATE_FIELDS
            if field in self.model_fields_set and getattr(self, field) is None
        ]
        if offending:
            joined = ", ".join(offending)
            raise ValueError(f"{joined} cannot be set to null")
        return self


class CommandRunCreateRequest(BaseModel):
    confirmed: bool = False
    worktree_id: UUID | None = None


class CommandReorderItem(BaseModel):
    id: UUID
    sort_order: int = Field(ge=0, le=1_000_000)


class CommandReorderRequest(BaseModel):
    items: list[CommandReorderItem] = Field(min_length=1, max_length=200)


class CustomCommandResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    command: str
    description: str | None
    icon: str | None
    hotkey: str | None
    requires_confirmation: bool
    role: CommandRole
    kind: CommandKind
    cwd: str | None
    port: int | None
    sort_order: int
    enabled: bool
    timeout_seconds: int | None
    health_path: str | None = None
    is_entry_point: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CommandRunResponse(BaseModel):
    id: UUID
    custom_command_id: UUID
    workspace_id: UUID
    user_id: UUID
    worktree_id: UUID | None
    cwd: str | None
    status: CommandRunStatus
    exit_code: int | None
    output: str
    started_at: datetime
    completed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class DetectedCommandResponse(BaseModel):
    """A *proposal*. Nothing is persisted until the user imports it."""

    name: str
    command: str
    role: CommandRole
    cwd: str | None
    port: int | None
    source_file: str
    reason: str


class DetectCommandsResponse(BaseModel):
    candidates: list[DetectedCommandResponse]


class ImportCommandItem(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    command: str = Field(min_length=1, max_length=4000)
    role: CommandRole = "custom"
    # None => the service defaults it from role (a `dev`-role candidate becomes `service`).
    kind: CommandKind | None = None
    cwd: str | None = Field(default=None, max_length=512)
    port: int | None = Field(default=None, ge=1, le=65535)

    @field_validator("cwd")
    @classmethod
    def normalize_cwd(cls, value: str | None) -> str | None:
        return normalize_subpath(value)


class ImportCommandsRequest(BaseModel):
    items: list[ImportCommandItem] = Field(min_length=1, max_length=60)


class RunPresetCommandResponse(BaseModel):
    name: str
    command: str
    role: CommandRole
    cwd: str | None
    port: int | None


class RunPresetResponse(BaseModel):
    slug: str
    name: str
    description: str
    commands: list[RunPresetCommandResponse]
