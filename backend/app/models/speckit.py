import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Spec Kit phases, in pipeline order.
SPEC_PHASES = (
    "constitution",
    "specify",
    "clarify",
    "plan",
    "analyze",
    "tasks",
    "implement",
)
SPEC_WORKFLOW_STATUSES = ("pending", "running", "awaiting_review", "completed", "error")
SPEC_PHASE_STATUSES = (
    "pending",
    "running",
    "awaiting_review",
    "approved",
    "changes_requested",
    "completed",
    "error",
)


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class SpecWorkflow(Base):
    __tablename__ = "spec_workflows"
    __table_args__ = (
        CheckConstraint(
            f"status in ({_sql_in_list(SPEC_WORKFLOW_STATUSES)})",
            name="ck_spec_workflows_status",
        ),
        Index("ix_spec_workflows_workspace_id", "workspace_id"),
        Index("ix_spec_workflows_status", "status"),
        Index("ix_spec_workflows_current_phase", "current_phase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    feature_name: Mapped[str] = mapped_column(String(200), nullable=False)
    feature_slug: Mapped[str] = mapped_column(String(220), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(String(220), nullable=False)
    spec_dir: Mapped[str] = mapped_column(String(255), nullable=False)
    worktree_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    current_phase: Mapped[str] = mapped_column(String(32), nullable=False, default="specify")
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

    phases: Mapped[list["SpecPhase"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SpecPhase(Base):
    __tablename__ = "spec_phases"
    __table_args__ = (
        CheckConstraint(
            f"phase in ({_sql_in_list(SPEC_PHASES)})",
            name="ck_spec_phases_phase",
        ),
        CheckConstraint(
            f"status in ({_sql_in_list(SPEC_PHASE_STATUSES)})",
            name="ck_spec_phases_status",
        ),
        UniqueConstraint("workflow_id", "phase", name="uq_spec_phases_workflow_phase"),
        Index("ix_spec_phases_workflow_id", "workflow_id"),
        Index("ix_spec_phases_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("spec_workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    artifact_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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

    workflow: Mapped[SpecWorkflow] = relationship(back_populates="phases")
