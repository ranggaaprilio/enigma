import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

WORKTREE_STATUSES = ("active", "removing")


def _sql_in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class WorkspaceWorktree(Base):
    """A git worktree attached to a workspace's clone.

    The base clone (``workspace.workspace_path`` on the default branch) is represented by a row
    with ``is_base=True``; feature worktrees live under the managed ``worktrees/`` sibling dir.
    """

    __tablename__ = "workspace_worktrees"
    __table_args__ = (
        CheckConstraint(
            f"status in ({_sql_in_list(WORKTREE_STATUSES)})",
            name="ck_workspace_worktrees_status",
        ),
        UniqueConstraint("workspace_id", "branch", name="uq_workspace_worktrees_workspace_branch"),
        UniqueConstraint("workspace_id", "path", name="uq_workspace_worktrees_workspace_path"),
        Index("ix_workspace_worktrees_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    branch: Mapped[str] = mapped_column(String(220), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    base_ref: Mapped[str] = mapped_column(String(220), nullable=False)
    is_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
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


class PullRequest(Base):
    """Lightweight cache of a GitHub Pull Request opened from a workspace worktree."""

    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("workspace_id", "number", name="uq_pull_requests_workspace_number"),
        Index("ix_pull_requests_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    worktree_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
        nullable=True,
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    head: Mapped[str] = mapped_column(String(220), nullable=False)
    base: Mapped[str] = mapped_column(String(220), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
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
