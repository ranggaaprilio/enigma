from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

AgentRunStatus = Literal["thinking", "executing", "completed", "error", "stopped"]
AgentEventType = Literal["status", "output_text", "tool_call", "tool_result", "error"]
AgentFileChangeStatus = Literal["created", "modified", "deleted", "renamed"]


class AgentRunCreateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=40_000)
    worktree_id: UUID | None = None

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Prompt cannot be empty")
        return stripped


class AgentEventResponse(BaseModel):
    id: UUID
    agent_run_id: UUID
    event_type: AgentEventType
    content: str
    tool_call_id: str | None = None
    metadata: dict
    created_at: datetime


class AgentRunResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    user_id: UUID
    prompt: str
    status: AgentRunStatus
    provider: str
    model: str
    tokens_used: int | None
    started_at: datetime
    completed_at: datetime | None
    error_message: str | None

    model_config = ConfigDict(from_attributes=True)


class AgentFileChangeResponse(BaseModel):
    id: UUID
    path: str
    status: AgentFileChangeStatus
    tool_call_id: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentFileChangeListResponse(BaseModel):
    items: list[AgentFileChangeResponse]
    total: int
    capped: bool


class AgentRunDetailResponse(AgentRunResponse):
    events: list[AgentEventResponse]
