from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

SpecPhaseName = Literal[
    "constitution",
    "specify",
    "clarify",
    "plan",
    "analyze",
    "tasks",
    "implement",
]
SpecWorkflowStatus = Literal["pending", "running", "awaiting_review", "completed", "error"]
SpecPhaseStatus = Literal[
    "pending",
    "running",
    "awaiting_review",
    "approved",
    "changes_requested",
    "completed",
    "error",
]


class SpecWorkflowCreateRequest(BaseModel):
    feature_name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=40_000)

    @field_validator("feature_name", "description")
    @classmethod
    def normalize(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value cannot be empty")
        return stripped


class RequestChangesRequest(BaseModel):
    notes: str = Field(min_length=1, max_length=20_000)

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Notes cannot be empty")
        return stripped


class SpecPhaseResponse(BaseModel):
    id: UUID
    workflow_id: UUID
    phase: SpecPhaseName
    status: SpecPhaseStatus
    artifact_path: str | None
    agent_run_id: UUID | None
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_notes: str | None
    attempt: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SpecWorkflowResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    user_id: UUID
    feature_name: str
    feature_slug: str
    description: str
    branch: str
    spec_dir: str
    worktree_path: str | None
    status: SpecWorkflowStatus
    current_phase: SpecPhaseName
    created_at: datetime
    updated_at: datetime
    phases: list[SpecPhaseResponse]

    model_config = ConfigDict(from_attributes=True)


class SpecArtifactResponse(BaseModel):
    phase: SpecPhaseName
    path: str | None
    content: str


class SpecArtifactItem(BaseModel):
    name: str
    path: str


class SpecArtifactListResponse(BaseModel):
    phase: SpecPhaseName
    items: list[SpecArtifactItem]


class OpenQuestion(BaseModel):
    id: str
    category: str | None = None
    severity: str | None = None
    location: str | None = None
    summary: str | None = None
    recommendation: str | None = None


class OpenQuestionsResponse(BaseModel):
    phase: SpecPhaseName
    items: list[OpenQuestion]


class AnalyzeAnswer(BaseModel):
    id: str = Field(min_length=1)
    answer: str = Field(min_length=1, max_length=20_000)

    @field_validator("id", "answer")
    @classmethod
    def normalize(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value cannot be empty")
        return stripped


class AnalyzeAnswersRequest(BaseModel):
    answers: list[AnalyzeAnswer] = Field(min_length=1)
