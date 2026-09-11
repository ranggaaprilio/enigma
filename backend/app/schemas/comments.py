from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

CommentTargetType = Literal["run", "spec_phase", "pr"]


class CommentCreateRequest(BaseModel):
    target_type: CommentTargetType
    target_id: UUID
    body: str = Field(min_length=1, max_length=10_000)

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Comment body cannot be empty")
        return stripped


class CommentResponse(BaseModel):
    id: UUID
    target_type: str
    target_id: UUID
    user_id: UUID
    body: str
    resolved_at: datetime | None
    resolved_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
