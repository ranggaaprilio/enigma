from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProjectRole = Literal["admin", "member", "viewer"]
TeamRole = Literal["lead", "member", "viewer"]


def _normalize_name(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("Value cannot be empty")
    return stripped


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _normalize_name(value)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_name(value)


class ProjectResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProjectMemberResponse(BaseModel):
    id: UUID
    project_id: UUID
    user_id: UUID
    role: ProjectRole
    joined_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AddProjectMemberRequest(BaseModel):
    user_id: UUID
    role: ProjectRole = "member"


class UpdateProjectMemberRoleRequest(BaseModel):
    role: ProjectRole


class TeamCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _normalize_name(value)


class TeamUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_name(value)


class TeamResponse(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    slug: str
    description: str | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamMemberResponse(BaseModel):
    id: UUID
    team_id: UUID
    user_id: UUID
    role: TeamRole
    joined_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AddTeamMemberRequest(BaseModel):
    user_id: UUID
    role: TeamRole = "member"


class UpdateTeamMemberRoleRequest(BaseModel):
    role: TeamRole
