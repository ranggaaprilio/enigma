import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CustomCommand, Workspace, WorkspaceEnvVar, WorkspaceTemplate
from app.models.project import TeamMember
from app.services.git_providers import sanitize_repository_url
from app.services.sandbox import SandboxRuntime, SandboxRuntimeError
from app.services.sandbox.availability import Availability, choose_runtime
from app.services.templates import TemplateService
from app.services.utils import slugify
from app.services.workspace_init import WorkspaceInitializationError, WorkspaceInitializer

ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SNAPSHOT_UNSET = object()
# Distinguishes "leave app_root alone" from "clear it back to the checkout root".
APP_ROOT_UNSET = object()
# Distinguishes "leave env_file_path alone" from "clear it back to unset (no file written)".
ENV_FILE_PATH_UNSET = object()


class WorkspaceAlreadyExistsError(Exception):
    pass


class WorkspaceNotFoundError(Exception):
    pass


class WorkspaceRuntimeError(Exception):
    pass


class InvalidEnvVarKeyError(Exception):
    pass


class InvalidEnvScopeError(Exception):
    """Raised when a ``custom_command_id`` is given that doesn't exist, or doesn't belong to
    the workspace an env-var operation targets."""

    pass


EnvVarWriteStatus = Literal["created", "updated", "unchanged"]


@dataclass(frozen=True)
class BulkItem:
    key: str
    status: Literal["created", "updated", "unchanged", "invalid"]
    message: str | None = None


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
        runtime: SandboxRuntime,
        initializer: WorkspaceInitializer | None = None,
        availability: Availability | None = None,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.runtime = runtime
        self.initializer = initializer or WorkspaceInitializer(runtime)
        # Checks the chosen provider before the row is written (Epic 06). None (unit tests
        # with a fake runtime) takes the requested provider or the default unchecked.
        self.availability = availability

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
        runtime: str | None = None,
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

        settings = self.runtime.settings
        if self.availability is not None:
            runtime = await choose_runtime(runtime, settings, self.availability)
        workspace = Workspace(
            owner_id=owner_id,
            team_id=team_id,
            name=name,
            slug=slugify(name, fallback="workspace"),
            repo_url=repo_url,
            source=source,
            template_id=template.id if template is not None else None,
            default_branch=default_branch,
            runtime=runtime or settings.sandbox_runtime_default,
            snapshot=snapshot,
            workspace_path=self.runtime.settings.sandbox_workspace_path,
            status="creating",
        )

        self.session.add(workspace)
        await self.session.flush()
        await self.session.commit()

        return await self._provision(workspace, template)

    async def _mark_error(self, workspace: Workspace) -> None:
        workspace.status = "error"
        self.session.add(workspace)
        await self.session.commit()

    async def _provision(
        self,
        workspace: Workspace,
        template: WorkspaceTemplate | None,
    ) -> Workspace:
        """Bring an already-persisted workspace row up to a running sandbox.

        Shared by `create` and `reprovision`. `SandboxRuntime.provision` looks the sandbox up
        by the name derived from the workspace id and reuses it when it exists, so calling
        this a second time repairs the row instead of leaking a second sandbox.

        The template/blank seed commit runs only into a sandbox that is new -- the provider
        returned a different id than the row had (none, for a workspace that never finished
        creating; another, when the old sandbox was removed behind our back) -- so a repair
        never writes over an existing working tree, and a replacement is never left empty.
        """
        previous = workspace.sandbox_id
        try:
            workspace.sandbox_id = await self.runtime.provision(workspace)
        except SandboxRuntimeError as error:
            await self._mark_error(workspace)
            raise WorkspaceRuntimeError(str(error)) from error

        # Template/blank workspaces get their first commit now so the sandbox is usable (and
        # worktree-capable) before any remote is connected.
        initialize = workspace.sandbox_id != previous
        try:
            if initialize and template is not None:
                await self.initializer.initialize_from_template(workspace, template)
            elif initialize and workspace.source == "blank":
                await self.initializer.initialize_blank(workspace)
        except WorkspaceInitializationError:
            await self._mark_error(workspace)
            raise

        workspace.status = "ready"
        self.session.add(workspace)
        await self.session.commit()
        await self.session.refresh(workspace)

        return workspace

    async def reprovision(self, workspace_id: UUID, owner_id: UUID) -> Workspace:
        """Retry provisioning for a workspace whose sandbox failed to come up.

        A create that fails at the runtime step leaves the row behind with status "error" and
        no sandbox id; nothing retries it, and `status` cannot recover it because there is no
        sandbox to query. This is the repair path — typically after correcting the snapshot.
        """
        workspace = await self.get_for_owner(workspace_id, owner_id)

        template: WorkspaceTemplate | None = None
        if workspace.source == "template" and workspace.template_id is not None:
            template = await TemplateService(self.session).get(workspace.template_id)

        workspace.status = "creating"
        self.session.add(workspace)
        await self.session.commit()

        return await self._provision(workspace, template)

    async def update(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        name: str | None = None,
        repo_url: str | None = None,
        default_branch: str | None = None,
        snapshot: str | None | object = SNAPSHOT_UNSET,
        app_root: str | None | object = APP_ROOT_UNSET,
        env_file_path: str | None | object = ENV_FILE_PATH_UNSET,
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
        if app_root is not APP_ROOT_UNSET:
            workspace.app_root = app_root or None
        if env_file_path is not ENV_FILE_PATH_UNSET:
            workspace.env_file_path = env_file_path or None
        self.session.add(workspace)
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def delete(self, workspace_id: UUID, owner_id: UUID) -> None:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        try:
            await self.runtime.delete(workspace)
        except SandboxRuntimeError as error:
            raise WorkspaceRuntimeError(str(error)) from error
        await self.session.delete(workspace)
        await self.session.commit()

    async def list_env_vars(self, workspace_id: UUID, owner_id: UUID) -> list[WorkspaceEnvVar]:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        result = await self.session.execute(
            select(WorkspaceEnvVar)
            .where(WorkspaceEnvVar.workspace_id == workspace.id)
            .order_by(WorkspaceEnvVar.custom_command_id.is_not(None), WorkspaceEnvVar.key)
        )
        return list(result.scalars().all())

    async def _validate_custom_command_id(
        self, workspace_id: UUID, custom_command_id: UUID | None
    ) -> None:
        if custom_command_id is None:
            return
        command = await self.session.get(CustomCommand, custom_command_id)
        if command is None or command.workspace_id != workspace_id:
            raise InvalidEnvScopeError

    async def upsert_env_var(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        key: str,
        value: str,
        *,
        custom_command_id: UUID | None = None,
    ) -> tuple[WorkspaceEnvVar, EnvVarWriteStatus]:
        normalized_key = validate_env_key(key)
        workspace = await self.get_for_owner(workspace_id, owner_id)
        await self._validate_custom_command_id(workspace.id, custom_command_id)
        env_var, write_status = await self._upsert_one(
            workspace.id, normalized_key, value, custom_command_id=custom_command_id
        )
        self.session.add(env_var)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(env_var)
        return env_var, write_status

    async def _upsert_one(
        self,
        workspace_id: UUID,
        normalized_key: str,
        value: str,
        *,
        custom_command_id: UUID | None,
    ) -> tuple[WorkspaceEnvVar, EnvVarWriteStatus]:
        """Write (without flush/commit) a single row, scoped by `(workspace_id, key,
        custom_command_id)`. Returns the row and whether it was created/updated/unchanged,
        determined by comparing the decrypted existing value -- never exposed beyond this
        comparison."""
        command_filter = (
            WorkspaceEnvVar.custom_command_id.is_(None)
            if custom_command_id is None
            else WorkspaceEnvVar.custom_command_id == custom_command_id
        )
        result = await self.session.execute(
            select(WorkspaceEnvVar).where(
                WorkspaceEnvVar.workspace_id == workspace_id,
                WorkspaceEnvVar.key == normalized_key,
                command_filter,
            )
        )
        env_var = result.scalar_one_or_none()
        encrypted_value = self.cipher.encrypt(value)
        if env_var is None:
            env_var = WorkspaceEnvVar(
                workspace_id=workspace_id,
                custom_command_id=custom_command_id,
                scope="service" if custom_command_id else "workspace",
                key=normalized_key,
                encrypted_value=encrypted_value,
            )
            return env_var, "created"

        existing_value = self.cipher.decrypt(env_var.encrypted_value)
        write_status: EnvVarWriteStatus = "unchanged" if existing_value == value else "updated"
        env_var.encrypted_value = encrypted_value
        return env_var, write_status

    async def bulk_upsert_env_vars(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        values: dict[str, str],
        *,
        custom_command_id: UUID | None = None,
        dry_run: bool = False,
    ) -> list[BulkItem]:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        await self._validate_custom_command_id(workspace.id, custom_command_id)

        valid_keys: dict[str, str] = {}
        items: list[BulkItem] = []
        for key, value in values.items():
            try:
                normalized_key = validate_env_key(key)
            except InvalidEnvVarKeyError:
                items.append(
                    BulkItem(
                        key=key,
                        status="invalid",
                        message="must use uppercase letters, numbers, and underscores",
                    )
                )
                continue
            valid_keys[normalized_key] = value

        if valid_keys:
            command_filter = (
                WorkspaceEnvVar.custom_command_id.is_(None)
                if custom_command_id is None
                else WorkspaceEnvVar.custom_command_id == custom_command_id
            )
            result = await self.session.execute(
                select(WorkspaceEnvVar).where(
                    WorkspaceEnvVar.workspace_id == workspace.id,
                    WorkspaceEnvVar.key.in_(list(valid_keys.keys())),
                    command_filter,
                )
            )
            existing_by_key = {row.key: row for row in result.scalars().all()}

            for normalized_key, value in valid_keys.items():
                env_var = existing_by_key.get(normalized_key)
                encrypted_value = self.cipher.encrypt(value)
                if env_var is None:
                    write_status: EnvVarWriteStatus = "created"
                    if not dry_run:
                        env_var = WorkspaceEnvVar(
                            workspace_id=workspace.id,
                            custom_command_id=custom_command_id,
                            scope="service" if custom_command_id else "workspace",
                            key=normalized_key,
                            encrypted_value=encrypted_value,
                        )
                        self.session.add(env_var)
                else:
                    existing_value = self.cipher.decrypt(env_var.encrypted_value)
                    write_status = "unchanged" if existing_value == value else "updated"
                    if not dry_run and write_status == "updated":
                        env_var.encrypted_value = encrypted_value
                items.append(BulkItem(key=normalized_key, status=write_status))

            if not dry_run:
                await self.session.flush()
                await self.session.commit()

        return items

    async def delete_env_var(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        key: str,
        *,
        custom_command_id: UUID | None = None,
    ) -> None:
        normalized_key = validate_env_key(key)
        workspace = await self.get_for_owner(workspace_id, owner_id)
        command_filter = (
            WorkspaceEnvVar.custom_command_id.is_(None)
            if custom_command_id is None
            else WorkspaceEnvVar.custom_command_id == custom_command_id
        )
        result = await self.session.execute(
            select(WorkspaceEnvVar).where(
                WorkspaceEnvVar.workspace_id == workspace.id,
                WorkspaceEnvVar.key == normalized_key,
                command_filter,
            )
        )
        env_var = result.scalar_one_or_none()
        if env_var is not None:
            await self.session.delete(env_var)
        await self.session.commit()

    async def bulk_delete_env_vars(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        keys: list[str],
        *,
        custom_command_id: UUID | None = None,
    ) -> tuple[list[str], list[str]]:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        await self._validate_custom_command_id(workspace.id, custom_command_id)

        deleted: list[str] = []
        missing: list[str] = []
        normalized_keys: list[str] = []
        for key in keys:
            try:
                normalized_keys.append(validate_env_key(key))
            except InvalidEnvVarKeyError:
                # Not a valid key shape, so it can never have been stored -- report it missing
                # rather than dropping it silently.
                missing.append(key)

        command_filter = (
            WorkspaceEnvVar.custom_command_id.is_(None)
            if custom_command_id is None
            else WorkspaceEnvVar.custom_command_id == custom_command_id
        )
        existing_by_key: dict[str, WorkspaceEnvVar] = {}
        if normalized_keys:
            result = await self.session.execute(
                select(WorkspaceEnvVar).where(
                    WorkspaceEnvVar.workspace_id == workspace.id,
                    WorkspaceEnvVar.key.in_(normalized_keys),
                    command_filter,
                )
            )
            existing_by_key = {row.key: row for row in result.scalars().all()}

        for key in normalized_keys:
            env_var = existing_by_key.get(key)
            if env_var is None:
                missing.append(key)
            else:
                deleted.append(key)
                await self.session.delete(env_var)

        await self.session.commit()
        return deleted, missing

    async def env_template(self, workspace_id: UUID, owner_id: UUID) -> str:
        """A `.env`-shaped template of every stored key (all scopes) for this workspace, with
        every value blanked -- never decrypted. Workspace-wide keys are grouped first, then one
        group per service with its own scoped overrides."""
        workspace = await self.get_for_owner(workspace_id, owner_id)
        result = await self.session.execute(
            select(WorkspaceEnvVar)
            .where(WorkspaceEnvVar.workspace_id == workspace.id)
            .order_by(WorkspaceEnvVar.custom_command_id.is_not(None), WorkspaceEnvVar.key)
        )
        env_vars = list(result.scalars().all())

        command_ids = {
            env_var.custom_command_id for env_var in env_vars if env_var.custom_command_id
        }
        command_names: dict[UUID, str] = {}
        if command_ids:
            commands_result = await self.session.execute(
                select(CustomCommand).where(CustomCommand.id.in_(command_ids))
            )
            command_names = {
                command.id: command.name for command in commands_result.scalars().all()
            }

        workspace_wide = [env_var for env_var in env_vars if env_var.custom_command_id is None]
        by_command: dict[UUID, list[WorkspaceEnvVar]] = {}
        for env_var in env_vars:
            if env_var.custom_command_id is not None:
                by_command.setdefault(env_var.custom_command_id, []).append(env_var)

        sections: list[str] = []
        if workspace_wide:
            lines = "\n".join(f"{env_var.key}=" for env_var in workspace_wide)
            sections.append(f"# Workspace-wide\n{lines}\n")
        for command_id, rows in by_command.items():
            name = command_names.get(command_id, "Unknown service")
            lines = "\n".join(f"{env_var.key}=" for env_var in rows)
            sections.append(f"# Service: {name} ({command_id})\n{lines}\n")

        return "\n".join(sections)
