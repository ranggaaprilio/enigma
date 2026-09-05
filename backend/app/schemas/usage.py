from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

UsageScope = Literal["workspace", "team", "project"]
UsageBreakdownDimension = Literal["project", "team", "workspace", "user"]


class UsageTotals(BaseModel):
    run_count: int
    input_tokens: int
    output_tokens: int
    tokens_used: int
    estimated_cost: float


class UsageSeriesPoint(BaseModel):
    date: datetime
    run_count: int
    tokens_used: int
    estimated_cost: float


class UsageBreakdownItem(BaseModel):
    id: UUID | None
    name: str
    run_count: int
    tokens_used: int
    estimated_cost: float


class UsageSummaryResponse(BaseModel):
    scope: UsageScope | None
    scope_id: UUID | None
    date_from: datetime
    date_to: datetime
    totals: UsageTotals
    series: list[UsageSeriesPoint]
    breakdown_dimension: UsageBreakdownDimension
    breakdown: list[UsageBreakdownItem]
    top_spenders: list[UsageBreakdownItem]
