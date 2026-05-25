from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

CommandRunStatus = Literal["running", "completed", "failed", "error"]


def normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class CustomCommandCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    command: str = Field(min_length=1, max_length=4000)
    description: str | None = Field(default=None, max_length=4000)
    icon: str | None = Field(default=None, max_length=80)
    hotkey: str | None = Field(default=None, max_length=80)
    requires_confirmation: bool = False

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


class CustomCommandUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    command: str | None = Field(default=None, min_length=1, max_length=4000)
    description: str | None = Field(default=None, max_length=4000)
    icon: str | None = Field(default=None, max_length=80)
    hotkey: str | None = Field(default=None, max_length=80)
    requires_confirmation: bool | None = None

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


class CommandRunCreateRequest(BaseModel):
    confirmed: bool = False


class CustomCommandResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    command: str
    description: str | None
    icon: str | None
    hotkey: str | None
    requires_confirmation: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CommandRunResponse(BaseModel):
    id: UUID
    custom_command_id: UUID
    workspace_id: UUID
    user_id: UUID
    status: CommandRunStatus
    exit_code: int | None
    output: str
    started_at: datetime
    completed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)
