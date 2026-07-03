from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.repositories import normalize_nonempty


class WorktreeCreateRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=200)
    base: str | None = Field(default=None, max_length=200)

    @field_validator("branch")
    @classmethod
    def normalize_branch(cls, value: str) -> str:
        return normalize_nonempty(value)

    @field_validator("base")
    @classmethod
    def normalize_base(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class WorktreeResponse(BaseModel):
    id: UUID
    branch: str
    path: str
    base_ref: str
    is_base: bool
    status: str
    is_clean: bool
    ahead: int
    behind: int


class WorktreeCommitRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        return normalize_nonempty(value)


class WorktreeSyncRequest(BaseModel):
    strategy: Literal["merge", "rebase"] = "merge"


class PullRequestCreateRequest(BaseModel):
    worktree_id: UUID | None = None
    head: str | None = Field(default=None, max_length=200)
    base: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    body: str | None = Field(default=None, max_length=60_000)
    draft: bool = False

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return normalize_nonempty(value)

    @field_validator("head", "base")
    @classmethod
    def normalize_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class PullRequestResponse(BaseModel):
    id: UUID
    worktree_id: UUID | None
    number: int
    url: str
    state: str
    head: str
    base: str
    title: str
