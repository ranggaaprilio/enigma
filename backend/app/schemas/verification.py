from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

VerificationRunStatus = Literal[
    "planning", "testing", "fixing", "observing", "review", "approved", "rejected", "error"
]
VerificationEvidenceKind = Literal["test", "observation", "screenshot", "fix"]
VerificationEvidenceStatus = Literal["passed", "failed", "observation_failed", "error"]


class VerificationRunCreateRequest(BaseModel):
    plan_ref: str = Field(min_length=1, max_length=4_000)
    test_command: str = Field(min_length=1, max_length=20_000)
    observe_port: int | None = Field(default=None, ge=1, le=65_535)
    cwd: str | None = Field(default=None, max_length=4_000)
    timeout: int | None = Field(default=None, ge=1, le=86_400)
    max_iterations: int = Field(default=1, ge=1, le=20)

    @field_validator("plan_ref", "test_command", "cwd")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class VerificationEvidenceResponse(BaseModel):
    id: UUID
    kind: VerificationEvidenceKind
    status: VerificationEvidenceStatus
    detail: dict
    path: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VerificationRunResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    plan_ref: str
    status: VerificationRunStatus
    spec_workflow_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VerificationRunDetailResponse(VerificationRunResponse):
    evidence: list[VerificationEvidenceResponse] = Field(default_factory=list)
