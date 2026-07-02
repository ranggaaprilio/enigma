from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

BudgetScope = Literal["workspace", "team", "project"]
BudgetPeriod = Literal["daily", "weekly", "monthly", "total"]
BudgetStatus = Literal["ok", "warning", "over"]


class BudgetCreateRequest(BaseModel):
    scope: BudgetScope
    scope_id: UUID
    period: BudgetPeriod = "monthly"
    limit_cost: float | None = Field(default=None, ge=0)
    limit_tokens: int | None = Field(default=None, ge=0)
    threshold_percent: int = Field(default=80, ge=0, le=100)
    enforce: bool = False

    @model_validator(mode="after")
    def _require_a_limit(self) -> "BudgetCreateRequest":
        if self.limit_cost is None and self.limit_tokens is None:
            raise ValueError("At least one of limit_cost or limit_tokens is required")
        return self


class BudgetUpdateRequest(BaseModel):
    period: BudgetPeriod | None = None
    limit_cost: float | None = Field(default=None, ge=0)
    limit_tokens: int | None = Field(default=None, ge=0)
    threshold_percent: int | None = Field(default=None, ge=0, le=100)
    enforce: bool | None = None


class BudgetResponse(BaseModel):
    id: UUID
    scope: BudgetScope
    scope_id: UUID
    period: BudgetPeriod
    limit_cost: float | None
    limit_tokens: int | None
    threshold_percent: int
    enforce: bool
    created_at: datetime
    updated_at: datetime
    # Current-period usage against the limit + derived status.
    current_cost: float
    current_tokens: int
    status: BudgetStatus
