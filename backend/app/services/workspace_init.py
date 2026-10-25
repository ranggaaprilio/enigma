"""First-commit scaffolding for template and blank workspaces.

Runs inside the sandbox right after provisioning. Both paths end with a fresh ``git init`` on the
workspace's default branch plus one commit, so everything downstream that assumes ``HEAD`` exists
(worktrees, discard, commit detail) works before the user has connected a remote.
"""

import shlex

from app.models import Workspace, WorkspaceTemplate
from app.services.git_providers import redact_secrets
from app.services.sandbox import RuntimeFileSystem, SandboxRuntime, SandboxRuntimeError

INIT_TIMEOUT_SECONDS = 180

# Config filenames mise looks for (mirrors ``dev_server.MISE_CONFIG_FILES``; duplicated to keep
# this module free of the dev-server import graph).
MISE_CONFIG_FILES = (
    "mise.toml",
    ".mise.toml",
    "mise/config.toml",
    ".config/mise/config.toml",
)

# Same fallback identity ``RepositoryService.commit`` uses (kept in sync by value; importing it
# would create an import cycle through ``workspaces``).
COMMIT_AUTHOR = "Enigma app"
COMMIT_EMAIL = "author@enigma.ai"


class WorkspaceInitializationError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class WorkspaceInitializer:
    def __init__(self, runtime: SandboxRuntime) -> None:
        self.runtime = runtime

    async def initialize_blank(self, workspace: Workspace) -> None:
        fs = await self._fs(workspace)
        path = workspace.workspace_path
        if await self._has_git(fs, path):
            return
        await self._ensure_dir(fs, path)
        readme = f"# {workspace.name}\n\nCreated with Enigma.\n"
        await self._upload(fs, readme, f"{path}/README.md")
        await self._init_and_commit(workspace, "Initial commit")

    async def initialize_from_template(
        self, workspace: Workspace, template: WorkspaceTemplate
    ) -> None:
        fs = await self._fs(workspace)
        path = workspace.workspace_path
        if await self._has_git(fs, path):
            return
        await self._ensure_dir(fs, path)
        clone_args = ["clone", "--depth", "1"]
        if template.source_ref:
            clone_args += ["--branch", template.source_ref]
        clone_args += ["--", template.source_repo_url, "."]
        await self._git(workspace, clone_args)
        # Fresh history: the template's commits and its remote are not the user's.
        await self._shell(workspace, "rm -rf .git")
        if template.mise_config and not await self._has_mise_config(fs, path):
            await self._upload(fs, template.mise_config, f"{path}/mise.toml")
        await self._init_and_commit(workspace, f"Initial commit from template {template.slug}")

    # ----- helpers ------------------------------------------------------------------

    async def _init_and_commit(self, workspace: Workspace, message: str) -> None:
        branch = workspace.default_branch
        init = await self._git(workspace, ["init", "-b", branch], check=False)
        if init.exit_code != 0:
            # git < 2.28 has no ``-b``; fall back to init + HEAD rewrite.
            await self._git(workspace, ["init"])
            await self._git(workspace, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"])
        await self._git(workspace, ["add", "-A"])
        await self._git(
            workspace,
            [
                "-c",
                f"user.name={COMMIT_AUTHOR}",
                "-c",
                f"user.email={COMMIT_EMAIL}",
                "commit",
                "-m",
                message,
            ],
        )

    async def _git(self, workspace: Workspace, args: list[str], *, check: bool = True):
        command = "git " + " ".join(shlex.quote(arg) for arg in args)
        return await self._shell(workspace, command, check=check)

    async def _shell(self, workspace: Workspace, command: str, *, check: bool = True):
        try:
            result = await self.runtime.exec(
                workspace,
                command,
                cwd=workspace.workspace_path,
                env={"GIT_TERMINAL_PROMPT": "0"},
                timeout=INIT_TIMEOUT_SECONDS,
            )
        except SandboxRuntimeError as error:
            raise WorkspaceInitializationError(str(error)) from error
        if check and result.exit_code != 0:
            output = result.stderr or result.stdout or f"{command} failed"
            raise WorkspaceInitializationError(redact_secrets(output))
        return result

    async def _fs(self, workspace: Workspace) -> RuntimeFileSystem:
        try:
            return await self.runtime.fs(workspace)
        except SandboxRuntimeError as error:
            raise WorkspaceInitializationError(str(error)) from error

    @staticmethod
    async def _has_git(fs: RuntimeFileSystem, path: str) -> bool:
        try:
            await fs.get_file_info(f"{path}/.git")
        except Exception:
            return False
        return True

    @staticmethod
    async def _has_mise_config(fs: RuntimeFileSystem, path: str) -> bool:
        for name in MISE_CONFIG_FILES:
            try:
                await fs.get_file_info(f"{path}/{name}")
            except Exception:
                continue
            return True
        return False

    @staticmethod
    async def _ensure_dir(fs: RuntimeFileSystem, path: str) -> None:
        try:
            await fs.get_file_info(path)
        except Exception:
            await fs.create_folder(path, "755")

    @staticmethod
    async def _upload(fs: RuntimeFileSystem, content: str, remote_path: str) -> None:
        try:
            await fs.upload_file(content.encode("utf-8"), remote_path)
        except Exception as error:
            raise WorkspaceInitializationError(f"Could not write {remote_path}: {error}") from error
