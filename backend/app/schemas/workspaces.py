from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

WorkspaceStatus = Literal["creating", "ready", "busy", "error", "stopped"]


def normalize_nonempty(value: str) -> str:
    return value.strip()


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    repo_url: str = Field(min_length=1, max_length=2048)
    default_branch: str = Field(default="main", min_length=1, max_length=120)

    @field_validator("name", "repo_url", "default_branch")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        stripped = normalize_nonempty(value)
        if not stripped:
            raise ValueError("Value cannot be empty")
        return stripped


class WorkspaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    repo_url: str | None = Field(default=None, min_length=1, max_length=2048)
    default_branch: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("name", "repo_url", "default_branch")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = normalize_nonempty(value)
        if not stripped:
            raise ValueError("Value cannot be empty")
        return stripped


class WorkspaceResponse(BaseModel):
    id: UUID
    owner_id: UUID
    name: str
    slug: str
    repo_url: str
    default_branch: str
    local_path: str | None
    status: WorkspaceStatus
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkspaceStatusResponse(BaseModel):
    id: UUID
    status: WorkspaceStatus
    local_path: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


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
