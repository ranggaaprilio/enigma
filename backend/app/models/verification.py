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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# One verification loop over a workspace; the lifecycle states a run moves through while
# the agent plans, tests, observes and finally is approved/rejected by the human reviewer.
VERIFICATION_RUN_STATUSES = (
    "planning",
    "testing",
    "fixing",
    "observing",
    "review",
    "approved",
    "rejected",
    "error",
)
# What a piece of evidence is: an automated test result, a human/agent observation, a
# screenshot captured during the run, or an agent fix attempt between test iterations.
VERIFICATION_EVIDENCE_KINDS = ("test", "observation", "screenshot", "fix")
VERIFICATION_EVIDENCE_STATUSES = ("passed", "failed", "observation_failed", "error")


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class VerificationRun(Base):
    __tablename__ = "verification_runs"
    __table_args__ = (
        CheckConstraint(
            f"status in ({_sql_in_list(VERIFICATION_RUN_STATUSES)})",
            name="ck_verification_runs_status",
        ),
        Index("ix_verification_runs_workspace_id", "workspace_id"),
        Index("ix_verification_runs_status", "status"),
        Index("ix_verification_runs_spec_workflow_id", "spec_workflow_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_ref: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Persisted loop inputs (test_command, observe_port, cwd, timeout, max_iterations) so any
    # process can resume the run from its row.
    criteria: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    # Who triggered the run; the fix agent's runs are attributed (and budgeted) to them.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Agent fix attempts made so far (bounded by criteria["max_iterations"] - 1).
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # The agent run of the current/last fix attempt, if any.
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    # The ordinary agent run whose code this run verifies, if any (Plan 9). Its fix agent runs in
    # that run's worktree and session.
    source_agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The Spec Kit workflow whose ``implement`` phase this run verifies, if any.
    spec_workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("spec_workflows.id", ondelete="SET NULL"), nullable=True
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

    evidence: Mapped[list["VerificationEvidence"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class VerificationEvidence(Base):
    __tablename__ = "verification_evidence"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in_list(VERIFICATION_EVIDENCE_KINDS)})",
            name="ck_verification_evidence_kind",
        ),
        CheckConstraint(
            f"status in ({_sql_in_list(VERIFICATION_EVIDENCE_STATUSES)})",
            name="ck_verification_evidence_status",
        ),
        Index("ix_verification_evidence_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run: Mapped[VerificationRun] = relationship(back_populates="evidence")


class VerificationArtifact(Base):
    """Object-store metadata for a binary artifact of one evidence row (a screenshot PNG).

    The bytes live in the S3-compatible artifact store under ``storage_key``; the bucket is
    private and objects are only ever read back through the authenticated API.
    """

    __tablename__ = "verification_artifacts"
    __table_args__ = (
        UniqueConstraint("evidence_id", name="uq_verification_artifacts_evidence_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("verification_evidence.id", ondelete="CASCADE"),
        nullable=False,
    )
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
