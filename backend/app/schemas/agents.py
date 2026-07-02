from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.pricing import estimate_cost

AgentRunStatus = Literal["thinking", "executing", "completed", "error", "stopped"]
AgentEventType = Literal["status", "output_text", "tool_call", "tool_result", "error"]
AgentFileChangeStatus = Literal["created", "modified", "deleted", "renamed"]
AgentSessionStatus = Literal["active", "archived"]


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
    session_id: UUID | None = None
    prompt: str
    status: AgentRunStatus
    provider: str
    model: str
    tokens_used: int | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float | None = None
    started_at: datetime
    completed_at: datetime | None
    error_message: str | None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def _fill_estimated_cost(self) -> "AgentRunResponse":
        # Compute-on-read fallback so runs recorded before cost tracking (or whose provider
        # was priced later) still surface a cost. Uses default pricing (no env overrides).
        if self.estimated_cost is None and (
            self.tokens_used is not None
            or self.input_tokens is not None
            or self.output_tokens is not None
        ):
            cost = estimate_cost(
                self.provider,
                self.model,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                total_tokens=self.tokens_used,
            )
            if cost is not None:
                self.estimated_cost = float(cost)
        return self


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


class AgentSessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class AgentSessionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    status: AgentSessionStatus | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class AgentSessionResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    user_id: UUID
    title: str | None
    status: AgentSessionStatus
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgentSessionDetailResponse(AgentSessionResponse):
    turns: list[AgentRunResponse]
