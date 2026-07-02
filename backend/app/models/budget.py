import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

USAGE_BUDGET_SCOPES = ("workspace", "team", "project")
USAGE_BUDGET_PERIODS = ("daily", "weekly", "monthly", "total")


class UsageBudget(Base):
    __tablename__ = "usage_budgets"
    __table_args__ = (
        CheckConstraint(
            "scope in ('workspace', 'team', 'project')",
            name="ck_usage_budgets_scope",
        ),
        CheckConstraint(
            "period in ('daily', 'weekly', 'monthly', 'total')",
            name="ck_usage_budgets_period",
        ),
        CheckConstraint(
            "limit_cost is not null or limit_tokens is not null",
            name="ck_usage_budgets_limit_present",
        ),
        CheckConstraint(
            "threshold_percent >= 0 and threshold_percent <= 100",
            name="ck_usage_budgets_threshold_range",
        ),
        UniqueConstraint("scope", "scope_id", "period", name="uq_usage_budgets_scope_period"),
        Index("ix_usage_budgets_scope_scope_id", "scope", "scope_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    period: Mapped[str] = mapped_column(String(20), nullable=False)
    limit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    limit_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    threshold_percent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=80,
        server_default="80",
    )
    enforce: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
