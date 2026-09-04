import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RepositoryCredential(Base):
    """The single credential a workspace uses to reach its git remote.

    ``auth_type`` selects which material is populated: a token (``encrypted_token``, optional
    ``username`` for hosts whose basic-auth username is not fixed) or a server-generated SSH
    deploy key (``encrypted_private_key`` + ``public_key``). Secret columns are Fernet-encrypted
    with ``WorkspaceSecretCipher``; ``public_key`` is safe to show to the user.
    """

    __tablename__ = "repository_credentials"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_repository_credentials_workspace_id"),
        Index("ix_repository_credentials_workspace_id", "workspace_id"),
        Index("ix_repository_credentials_provider", "provider"),
        CheckConstraint(
            "provider in ('github', 'gitlab', 'bitbucket', 'generic')",
            name="ck_repository_credentials_provider",
        ),
        CheckConstraint(
            "auth_type in ('personal_access_token', 'ssh_key')",
            name="ck_repository_credentials_auth_type",
        ),
        CheckConstraint(
            "(auth_type = 'personal_access_token' AND encrypted_token IS NOT NULL) OR "
            "(auth_type = 'ssh_key' AND encrypted_private_key IS NOT NULL "
            "AND public_key IS NOT NULL)",
            name="ck_repository_credentials_material",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(80), nullable=False)
    encrypted_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
