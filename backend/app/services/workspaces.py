import re
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Workspace, WorkspaceEnvVar, WorkspaceTemplate
from app.models.project import TeamMember
from app.services.daytona_runtime import DaytonaRuntime, DaytonaRuntimeError
from app.services.git_providers import sanitize_repository_url
from app.services.templates import TemplateService
from app.services.utils import slugify
from app.services.workspace_init import WorkspaceInitializationError, WorkspaceInitializer

ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SNAPSHOT_UNSET = object()


class WorkspaceAlreadyExistsError(Exception):
    pass


class WorkspaceNotFoundError(Exception):
    pass


class WorkspaceRuntimeError(Exception):
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
        cipher: WorkspaceSecretCipher,
        runtime: DaytonaRuntime,
        initializer: WorkspaceInitializer | None = None,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.runtime = runtime
        self.initializer = initializer or WorkspaceInitializer(runtime)

    async def _is_team_member(self, team_id: UUID, user_id: UUID) -> bool:
        result = await self.session.execute(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def list_for_owner(self, owner_id: UUID) -> list[Workspace]:
        result = await self.session.execute(
            select(Workspace).where(Workspace.owner_id == owner_id).order_by(Workspace.created_at)
        )
        return list(result.scalars().all())

    async def list_for_team(self, team_id: UUID, user_id: UUID) -> list[Workspace]:
        if not await self._is_team_member(team_id, user_id):
            raise WorkspaceNotFoundError
        result = await self.session.execute(
            select(Workspace)
            .where(Workspace.team_id == team_id)
            .order_by(Workspace.created_at)
        )
        return list(result.scalars().all())

    async def get_for_owner(self, workspace_id: UUID, owner_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError
        if workspace.owner_id == owner_id:
            return workspace
        if workspace.team_id is not None and await self._is_team_member(
            workspace.team_id, owner_id
        ):
            return workspace
        raise WorkspaceNotFoundError

    async def create(
        self,
        owner_id: UUID,
        name: str,
        repo_url: str | None,
        default_branch: str,
        snapshot: str | None = None,
        team_id: UUID | None = None,
        *,
        source: str = "repository",
        template_id: UUID | None = None,
    ) -> Workspace:
        template: WorkspaceTemplate | None = None
        if source == "repository":
            if not repo_url:
                raise ValueError("repo_url is required for a repository workspace")
            repo_url = sanitize_repository_url(repo_url)
        elif source == "template":
            if template_id is None:
                raise ValueError("template_id is required for a template workspace")
            template = await TemplateService(self.session).get(template_id)
            repo_url = None
            default_branch = template.default_branch
        elif source == "blank":
            repo_url = None
        else:
            raise ValueError(f"Unknown workspace source '{source}'")

        if team_id is None:
            result = await self.session.execute(
                select(Workspace.id).where(
                    Workspace.owner_id == owner_id, Workspace.team_id.is_(None)
                )
            )
            if result.scalar_one_or_none() is not None:
                raise WorkspaceAlreadyExistsError

        workspace = Workspace(
            owner_id=owner_id,
            team_id=team_id,
            name=name,
            slug=slugify(name, fallback="workspace"),
            repo_url=repo_url,
            source=source,
            template_id=template.id if template is not None else None,
            default_branch=default_branch,
            runtime="daytona",
            snapshot=snapshot,
            workspace_path=self.runtime.settings.daytona_workspace_path,
            status="creating",
        )

        self.session.add(workspace)
        await self.session.flush()
        await self.session.commit()

        try:
            sandbox = await self.runtime.provision(workspace.id, owner_id, snapshot)
            workspace.daytona_sandbox_id = sandbox.id
        except DaytonaRuntimeError as error:
            workspace.status = "error"
            self.session.add(workspace)
            await self.session.commit()
            raise WorkspaceRuntimeError(str(error)) from error

        # Template/blank workspaces get their first commit now so the sandbox is usable (and
        # worktree-capable) before any remote is connected.
        try:
            if template is not None:
                await self.initializer.initialize_from_template(workspace, template)
            elif source == "blank":
                await self.initializer.initialize_blank(workspace)
        except WorkspaceInitializationError:
            workspace.status = "error"
            self.session.add(workspace)
            await self.session.commit()
            raise

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
        snapshot: str | None | object = SNAPSHOT_UNSET,
    ) -> Workspace:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        if name is not None:
            workspace.name = name
            workspace.slug = slugify(name)
        if repo_url is not None:
            workspace.repo_url = sanitize_repository_url(repo_url)
        if default_branch is not None:
            workspace.default_branch = default_branch
        if snapshot is not SNAPSHOT_UNSET:
            workspace.snapshot = snapshot
        self.session.add(workspace)
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def delete(self, workspace_id: UUID, owner_id: UUID) -> None:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        try:
            await self.runtime.delete(workspace)
        except DaytonaRuntimeError as error:
            raise WorkspaceRuntimeError(str(error)) from error
        await self.session.delete(workspace)
        await self.session.commit()

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
