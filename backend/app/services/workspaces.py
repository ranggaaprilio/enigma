import re
import shutil
from pathlib import Path
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Workspace, WorkspaceEnvVar

ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


class WorkspaceAlreadyExistsError(Exception):
    pass


class WorkspaceNotFoundError(Exception):
    pass


class WorkspaceDirectoryError(Exception):
    pass


class InvalidEnvVarKeyError(Exception):
    pass


class WorkspaceSecretCipher:
    def __init__(self, key: str) -> None:
        self.fernet = Fernet(key.encode("utf-8"))

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_value: str) -> str | None:
        try:
            return self.fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            return None


def slugify(value: str) -> str:
    slug = SLUG_PATTERN.sub("-", value.strip().lower()).strip("-")
    return slug or "workspace"


def validate_env_key(key: str) -> str:
    normalized = key.strip().upper()
    if ENV_KEY_PATTERN.fullmatch(normalized) is None:
        raise InvalidEnvVarKeyError
    return normalized


def mask_env_value() -> str:
    return "********"


class SqlAlchemyWorkspaceService:
    def __init__(
        self,
        session: AsyncSession,
        workspace_root: Path,
        cipher: WorkspaceSecretCipher,
    ) -> None:
        self.session = session
        self.workspace_root = workspace_root
        self.cipher = cipher

    async def list_for_owner(self, owner_id: UUID) -> list[Workspace]:
        result = await self.session.execute(
            select(Workspace).where(Workspace.owner_id == owner_id).order_by(Workspace.created_at)
        )
        return list(result.scalars().all())

    async def get_for_owner(self, workspace_id: UUID, owner_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError
        return workspace

    async def create(
        self,
        owner_id: UUID,
        name: str,
        repo_url: str,
        default_branch: str,
    ) -> Workspace:
        result = await self.session.execute(
            select(Workspace.id).where(Workspace.owner_id == owner_id)
        )
        if result.scalar_one_or_none() is not None:
            raise WorkspaceAlreadyExistsError

        workspace = Workspace(
            owner_id=owner_id,
            name=name,
            slug=slugify(name),
            repo_url=repo_url,
            default_branch=default_branch,
            status="creating",
        )

        self.session.add(workspace)
        await self.session.flush()
        await self.session.commit()

        workspace_path = self.workspace_root / str(owner_id) / str(workspace.id)
        workspace.local_path = str(workspace_path)

        try:
            workspace_path.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            workspace.status = "error"
            self.session.add(workspace)
            await self.session.commit()
            raise WorkspaceDirectoryError from error

        workspace.status = "ready"
        self.session.add(workspace)
        await self.session.commit()
        await self.session.refresh(workspace)

        return workspace

    async def update(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        name: str | None = None,
        repo_url: str | None = None,
        default_branch: str | None = None,
    ) -> Workspace:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        if name is not None:
            workspace.name = name
            workspace.slug = slugify(name)
        if repo_url is not None:
            workspace.repo_url = repo_url
        if default_branch is not None:
            workspace.default_branch = default_branch
        self.session.add(workspace)
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def delete(self, workspace_id: UUID, owner_id: UUID) -> None:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        local_path = workspace.local_path
        await self.session.delete(workspace)
        await self.session.commit()

        if local_path:
            self._delete_workspace_directory(local_path)

    def _delete_workspace_directory(self, local_path: str) -> None:
        root = self.workspace_root.resolve()
        path = Path(local_path).resolve()
        if path == root or root not in path.parents:
            return
        shutil.rmtree(path, ignore_errors=True)

    async def list_env_vars(self, workspace_id: UUID, owner_id: UUID) -> list[WorkspaceEnvVar]:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        result = await self.session.execute(
            select(WorkspaceEnvVar)
            .where(WorkspaceEnvVar.workspace_id == workspace.id)
            .order_by(WorkspaceEnvVar.key)
        )
        return list(result.scalars().all())

    async def upsert_env_var(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        key: str,
        value: str,
    ) -> WorkspaceEnvVar:
        normalized_key = validate_env_key(key)
        workspace = await self.get_for_owner(workspace_id, owner_id)
        result = await self.session.execute(
            select(WorkspaceEnvVar).where(
                WorkspaceEnvVar.workspace_id == workspace.id,
                WorkspaceEnvVar.key == normalized_key,
            )
        )
        env_var = result.scalar_one_or_none()
        encrypted_value = self.cipher.encrypt(value)
        if env_var is None:
            env_var = WorkspaceEnvVar(
                workspace_id=workspace.id,
                key=normalized_key,
                encrypted_value=encrypted_value,
            )
        else:
            env_var.encrypted_value = encrypted_value
        self.session.add(env_var)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(env_var)
        return env_var

    async def delete_env_var(self, workspace_id: UUID, owner_id: UUID, key: str) -> None:
        normalized_key = validate_env_key(key)
        workspace = await self.get_for_owner(workspace_id, owner_id)
        result = await self.session.execute(
            select(WorkspaceEnvVar).where(
                WorkspaceEnvVar.workspace_id == workspace.id,
                WorkspaceEnvVar.key == normalized_key,
            )
        )
        env_var = result.scalar_one_or_none()
        if env_var is not None:
            await self.session.delete(env_var)
        await self.session.commit()
