from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

TerminalSessionStatus = Literal["active", "stopped", "error"]


class TerminalSessionCreateRequest(BaseModel):
    name: str = Field(default="Terminal", min_length=1, max_length=160)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value cannot be empty")
        return stripped


class TerminalSessionResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    user_id: UUID
    name: str
    tmux_session_name: str
    status: TerminalSessionStatus
    created_at: datetime
    last_active_at: datetime

    model_config = ConfigDict(from_attributes=True)
