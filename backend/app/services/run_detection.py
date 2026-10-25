"""Propose run-configuration entries by reading a repository's manifests.

Detection is **read-only and never executes repository code**: manifests are parsed
(``tomllib``/``json``/regex), never evaluated. In particular a ``Makefile`` is scanned for target
names rather than run, and mise tasks are read out of ``mise.toml`` instead of shelling out to
``mise task ls`` — which would execute ``mise`` (and imply ``mise trust``) against a repository the
user has not vetted yet.

Nothing here is persisted. ``detect`` returns *candidates*; the user chooses which become registry
entries, which is why every candidate carries the ``source_file`` it came from and a short
``reason`` the UI can show.

The IO half is deliberately thin — :func:`candidates_from_manifests` is a pure function over
``{relative path: file content}``, so the interesting logic is testable without a sandbox.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Workspace
from app.services.proxy_detection import (
    PROXY_CONFIG_FILES,
    ProxyConfig,
    proxy_configs_from_manifests,
)
from app.services.proxy_detection import (
    angular_proxy_config_path as _angular_proxy_config_path,
)
from app.services.run_paths import join_subpath, resolve_worktree, validate_relative_subpath
from app.services.sandbox import (
    SandboxRuntime,
    SandboxRuntimeError,
    get_sandbox_runtime,
)
from app.services.workspaces import WorkspaceNotFoundError

logger = structlog.get_logger(__name__)


class RunDetectionUnavailableError(Exception):
    """The sandbox could not be read, so nothing can be proposed."""


# Bounded reads: a manifest far larger than this is not a manifest we can use.
MAX_MANIFEST_BYTES = 64_000
# Guards against a monorepo with hundreds of packages producing an unusable review dialog.
MAX_CANDIDATES = 60
MAX_WORKSPACE_PACKAGES = 40

_DELIMITER = "<<<ENIGMA-FILE:"

# Manifests worth reading at the detection root.
ROOT_MANIFESTS = (
    "package.json",
    "pnpm-workspace.yaml",
    "turbo.json",
    "nx.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "uv.lock",
    "poetry.lock",
    "mise.toml",
    ".mise.toml",
    "mise/config.toml",
    ".config/mise/config.toml",
    "pyproject.toml",
    "requirements.txt",
    "manage.py",
    "alembic.ini",
    "Makefile",
    "go.mod",
    "Cargo.toml",
    "composer.json",
    "artisan",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)

# Script/task names that map onto a lifecycle role. Anything else is `custom`.
_ROLE_BY_NAME = {
    "dev": "dev",
    "start": "dev",
    "serve": "dev",
    "develop": "dev",
    "install": "install",
    "bootstrap": "install",
    "setup": "install",
    "test": "test",
    "tests": "test",
    "lint": "lint",
    "format": "lint",
    "typecheck": "lint",
    "migrate": "migrate",
    "migration": "migrate",
    "migrations": "migrate",
    "db:migrate": "migrate",
    "seed": "seed",
    "db:seed": "seed",
}

# Default ports for frameworks that do not state one on the command line.
_PORT_BY_SIGNATURE = (
    ("vite", 5173),
    ("next", 3000),
    ("react-scripts", 3000),
    ("nuxt", 3000),
    ("remix", 3000),
    ("ng serve", 4200),
    ("uvicorn", 8000),
    ("fastapi", 8000),
    ("manage.py runserver", 8000),
    ("gunicorn", 8000),
    ("flask", 5000),
    ("artisan serve", 8000),
    ("spring-boot", 8080),
    ("bootrun", 8080),
    ("rails", 3000),
)

_EXPLICIT_PORT = re.compile(r"(?:--port[= ]|-p[= ]|:)(\d{2,5})\b")
# A workspace glob we are willing to hand to the sandbox shell *unquoted* so it expands. Anything
# outside this set (quotes, `$`, `;`, `!` negations, spaces) is dropped rather than escaped —
# `shlex.quote` would neutralise the `*`, and a negation is not a place packages live anyway.
_SAFE_WORKSPACE_GLOB = re.compile(r"[A-Za-z0-9_@./*-]+")
# A Makefile target line: `build:` or `test: deps`. Excludes `.PHONY`, variables and recipes.
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", re.MULTILINE)


@dataclass(frozen=True)
class DetectedCommand:
    """One proposal. Never persisted until the user accepts it."""

    name: str
    command: str
    role: str
    cwd: str | None
    port: int | None
    source_file: str
    reason: str


@dataclass
class _Manifests:
    """The files detection managed to read, keyed by path relative to the detection root."""

    files: dict[str, str] = field(default_factory=dict)

    def get(self, path: str) -> str | None:
        return self.files.get(path)

    def has(self, path: str) -> bool:
        return path in self.files


def infer_role(name: str) -> str:
    """Map a script/task name onto a lifecycle role, falling back to ``custom``."""
    lowered = name.strip().lower()
    if lowered in _ROLE_BY_NAME:
        return _ROLE_BY_NAME[lowered]
    # `dev:api`, `test:unit`, `lint-fix` … take the role of their prefix.
    prefix = re.split(r"[:_-]", lowered, maxsplit=1)[0]
    return _ROLE_BY_NAME.get(prefix, "custom")


def infer_port(command: str) -> int | None:
    """An explicit ``--port``/``-p`` wins; otherwise fall back to the framework's default."""
    explicit = _EXPLICIT_PORT.search(command)
    if explicit:
        port = int(explicit.group(1))
        if 1 <= port <= 65535:
            return port
    lowered = command.lower()
    for signature, port in _PORT_BY_SIGNATURE:
        if signature in lowered:
            return port
    return None


def framework_signature(command: str) -> str | None:
    """The matched key from `_PORT_BY_SIGNATURE` (e.g. ``"vite"``, ``"uvicorn"``), or `None`.
    Epic 13's composition service classifies a service as web/api from this single source of
    truth, rather than a second hardcoded port/name table."""
    lowered = command.lower()
    for signature, _port in _PORT_BY_SIGNATURE:
        if signature in lowered:
            return signature
    return None


def _lockfile_manager(manifests: _Manifests, prefix: str) -> str | None:
    if manifests.has(f"{prefix}pnpm-lock.yaml"):
        return "pnpm"
    if manifests.has(f"{prefix}yarn.lock"):
        return "yarn"
    if manifests.has(f"{prefix}package-lock.json"):
        return "npm"
    return None


def _package_manager(manifests: _Manifests, prefix: str) -> tuple[str, str]:
    """Pick the package manager from the lockfile that is actually committed.

    Returns ``(manager, reason)``. A monorepo package has no lockfile of its own — the single
    ``pnpm-lock.yaml``/``yarn.lock`` sits at the workspace root — so the root's lockfile decides
    for every package. Without that fallback a pnpm workspace's packages would be proposed
    ``npm install``, which writes a stray ``package-lock.json`` and a second ``node_modules``.
    """
    local = _lockfile_manager(manifests, prefix)
    if local:
        return local, f"{local} lockfile"
    if prefix:
        root = _lockfile_manager(manifests, "")
        if root:
            return root, f"{root} workspace lockfile"
    return "npm", "no lockfile"


def _node_candidates(
    manifests: _Manifests, manifest_path: str, cwd: str | None
) -> list[DetectedCommand]:
    raw = manifests.get(manifest_path)
    if raw is None:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.info("run_detection.unparsable", source=manifest_path)
        return []
    if not isinstance(parsed, dict):
        return []

    prefix = f"{cwd}/" if cwd else ""
    manager, manager_reason = _package_manager(manifests, prefix)
    found: list[DetectedCommand] = [
        DetectedCommand(
            name="install",
            command=f"{manager} install",
            role="install",
            cwd=cwd,
            port=None,
            source_file=manifest_path,
            reason=f"{manifest_path} with a {manager_reason}",
        )
    ]

    scripts = parsed.get("scripts")
    if isinstance(scripts, dict):
        for name, body in scripts.items():
            if not isinstance(name, str) or not isinstance(body, str):
                continue
            # `build` is a packaging step, not something to run or serve.
            if name.strip().lower() in {"build", "prepare", "postinstall", "preinstall"}:
                continue
            command = f"{manager} run {name}"
            found.append(
                DetectedCommand(
                    name=name,
                    command=command,
                    role=infer_role(name),
                    cwd=cwd,
                    port=infer_port(body),
                    source_file=manifest_path,
                    reason=f'"{name}" in {manifest_path} scripts',
                )
            )
    return found


def _workspace_globs(manifests: _Manifests) -> list[str]:
    """Glob patterns declaring where a monorepo's packages live."""
    globs: list[str] = []

    raw_pnpm = manifests.get("pnpm-workspace.yaml")
    if raw_pnpm:
        # A deliberately tiny reader for the one shape that matters: a `packages:` list of
        # scalars. Pulling in a YAML dependency to read six lines is not worth it.
        in_packages = False
        for line in raw_pnpm.splitlines():
            stripped = line.strip()
            if stripped.startswith("packages:"):
                in_packages = True
                continue
            if in_packages:
                if stripped.startswith("- "):
                    globs.append(stripped[2:].strip().strip("'\""))
                elif stripped and not stripped.startswith("#"):
                    break

    raw_package = manifests.get("package.json")
    if raw_package:
        try:
            parsed = json.loads(raw_package)
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        declared = parsed.get("workspaces") if isinstance(parsed, dict) else None
        if isinstance(declared, dict):
            declared = declared.get("packages")
        if isinstance(declared, list):
            globs.extend(entry for entry in declared if isinstance(entry, str))

    return [
        glob
        for glob in globs
        if glob
        and ".." not in glob
        and not glob.startswith("/")
        and _SAFE_WORKSPACE_GLOB.fullmatch(glob)
    ]


def _python_installer(manifests: _Manifests, prefix: str, pyproject: dict) -> tuple[str, str]:
    """Choose the installer from evidence the project actually uses that tool.

    A lockfile or a ``[tool.<name>]`` table is that evidence. A substring test over the whole
    document is not: ``"uv" in ...`` is true for every project that depends on ``uvicorn``, which
    sent plain pip FastAPI projects to ``uv sync``.
    """
    tool = pyproject.get("tool") if isinstance(pyproject, dict) else None
    tool = tool if isinstance(tool, dict) else {}
    if manifests.has(f"{prefix}uv.lock") or "uv" in tool:
        return "uv sync", "is managed by uv"
    if manifests.has(f"{prefix}poetry.lock") or "poetry" in tool:
        return "poetry install", "is managed by Poetry"
    return "pip install -e .", "declares a Python project"


def _python_candidates(manifests: _Manifests, cwd: str | None) -> list[DetectedCommand]:
    prefix = f"{cwd}/" if cwd else ""
    found: list[DetectedCommand] = []

    pyproject_path = f"{prefix}pyproject.toml"
    raw = manifests.get(pyproject_path)
    dependencies = ""
    if raw is not None:
        try:
            parsed = tomllib.loads(raw)
        except tomllib.TOMLDecodeError:
            parsed = {}
            logger.info("run_detection.unparsable", source=pyproject_path)
        dependencies = json.dumps(parsed).lower()
        install_command, install_reason = _python_installer(manifests, prefix, parsed)
        found.append(
            DetectedCommand(
                name="install",
                command=install_command,
                role="install",
                cwd=cwd,
                port=None,
                source_file=pyproject_path,
                reason=f"{pyproject_path} {install_reason}",
            )
        )
        if "fastapi" in dependencies or "uvicorn" in dependencies:
            found.append(
                DetectedCommand(
                    name="dev:api",
                    command="uvicorn app.main:app --reload --host 0.0.0.0 --port 8000",
                    role="dev",
                    cwd=cwd,
                    port=8000,
                    source_file=pyproject_path,
                    reason=f"{pyproject_path} depends on FastAPI/uvicorn",
                )
            )
        if "pytest" in dependencies:
            found.append(
                DetectedCommand(
                    name="test",
                    command="pytest",
                    role="test",
                    cwd=cwd,
                    port=None,
                    source_file=pyproject_path,
                    reason=f"{pyproject_path} depends on pytest",
                )
            )

    requirements_path = f"{prefix}requirements.txt"
    if manifests.has(requirements_path) and not manifests.has(pyproject_path):
        found.append(
            DetectedCommand(
                name="install",
                command="pip install -r requirements.txt",
                role="install",
                cwd=cwd,
                port=None,
                source_file=requirements_path,
                reason=f"{requirements_path} lists dependencies",
            )
        )

    manage_path = f"{prefix}manage.py"
    if manifests.has(manage_path):
        found.append(
            DetectedCommand(
                name="dev",
                command="python manage.py runserver 0.0.0.0:8000",
                role="dev",
                cwd=cwd,
                port=8000,
                source_file=manage_path,
                reason=f"{manage_path} is a Django entry point",
            )
        )
        found.append(
            DetectedCommand(
                name="migrate",
                command="python manage.py migrate",
                role="migrate",
                cwd=cwd,
                port=None,
                source_file=manage_path,
                reason=f"{manage_path} is a Django entry point",
            )
        )

    alembic_path = f"{prefix}alembic.ini"
    if manifests.has(alembic_path):
        found.append(
            DetectedCommand(
                name="migrate",
                command="alembic upgrade head",
                role="migrate",
                cwd=cwd,
                port=None,
                source_file=alembic_path,
                reason=f"{alembic_path} configures Alembic migrations",
            )
        )
    return found


def _mise_candidates(manifests: _Manifests) -> list[DetectedCommand]:
    """Read ``[tasks.*]`` straight out of the config — never `mise task ls`, which executes mise."""
    for path in ("mise.toml", ".mise.toml", "mise/config.toml", ".config/mise/config.toml"):
        raw = manifests.get(path)
        if raw is None:
            continue
        try:
            parsed = tomllib.loads(raw)
        except tomllib.TOMLDecodeError:
            logger.info("run_detection.unparsable", source=path)
            continue
        tasks = parsed.get("tasks")
        if not isinstance(tasks, dict):
            continue
        return [
            DetectedCommand(
                name=name,
                command=f"mise run {name}",
                role=infer_role(name),
                cwd=None,
                port=infer_port(
                    body.get("run", "") if isinstance(body, dict) else str(body)
                ),
                source_file=path,
                reason=f'"[tasks.{name}]" in {path}',
            )
            for name, body in tasks.items()
            if isinstance(name, str)
        ]
    return []


def _makefile_candidates(manifests: _Manifests) -> list[DetectedCommand]:
    """Scan target names only. A Makefile is *parsed*, never evaluated."""
    raw = manifests.get("Makefile")
    if raw is None:
        return []
    seen: list[DetectedCommand] = []
    for name in dict.fromkeys(_MAKE_TARGET.findall(raw)):
        if name.startswith("."):
            continue
        seen.append(
            DetectedCommand(
                name=name,
                command=f"make {name}",
                role=infer_role(name),
                cwd=None,
                port=None,
                source_file="Makefile",
                reason=f'"{name}" target in Makefile',
            )
        )
    return seen


def _other_ecosystem_candidates(manifests: _Manifests) -> list[DetectedCommand]:
    found: list[DetectedCommand] = []
    if manifests.has("go.mod"):
        found += [
            DetectedCommand("dev", "go run ./...", "dev", None, None, "go.mod", "Go module"),
            DetectedCommand("test", "go test ./...", "test", None, None, "go.mod", "Go module"),
        ]
    if manifests.has("Cargo.toml"):
        found += [
            DetectedCommand("dev", "cargo run", "dev", None, None, "Cargo.toml", "Cargo crate"),
            DetectedCommand("test", "cargo test", "test", None, None, "Cargo.toml", "Cargo crate"),
        ]
    if manifests.has("composer.json"):
        found.append(
            DetectedCommand(
                "install", "composer install", "install", None, None,
                "composer.json", "PHP project",
            )
        )
        if manifests.has("artisan"):
            found += [
                DetectedCommand(
                    "dev", "php artisan serve --host 0.0.0.0 --port 8000", "dev", None, 8000,
                    "artisan", "Laravel entry point",
                ),
                DetectedCommand(
                    "migrate", "php artisan migrate", "migrate", None, None,
                    "artisan", "Laravel entry point",
                ),
            ]
    if manifests.has("pom.xml"):
        found += [
            DetectedCommand(
                "dev", "./mvnw spring-boot:run", "dev", None, 8080, "pom.xml", "Maven project"
            ),
            DetectedCommand("test", "./mvnw test", "test", None, None, "pom.xml", "Maven project"),
        ]
    for gradle in ("build.gradle", "build.gradle.kts"):
        if manifests.has(gradle):
            found += [
                DetectedCommand(
                    "dev", "./gradlew bootRun", "dev", None, 8080, gradle, "Gradle project"
                ),
                DetectedCommand(
                    "test", "./gradlew test", "test", None, None, gradle, "Gradle project"
                ),
            ]
            break
    return found


def candidates_from_manifests(
    files: dict[str, str], *, package_dirs: tuple[str, ...] = ()
) -> list[DetectedCommand]:
    """Turn raw manifest contents into proposals. Pure — no IO, no sandbox.

    ``files`` maps a path *relative to the detection root* to its content. ``package_dirs`` are
    monorepo package directories whose ``package.json`` files are also present in ``files``.
    """
    manifests = _Manifests(dict(files))
    found: list[DetectedCommand] = []

    found += _mise_candidates(manifests)
    found += _node_candidates(manifests, "package.json", None)
    for directory in package_dirs:
        found += _node_candidates(manifests, f"{directory}/package.json", directory)
    found += _python_candidates(manifests, None)
    for directory in package_dirs:
        found += _python_candidates(manifests, directory)
    found += _makefile_candidates(manifests)
    found += _other_ecosystem_candidates(manifests)

    # Two manifests can propose the same thing (a `test` script and a `test` Makefile target);
    # keep the first, which comes from the more specific source.
    deduped: dict[tuple[str | None, str], DetectedCommand] = {}
    for candidate in found:
        deduped.setdefault((candidate.cwd, candidate.command), candidate)
    return list(deduped.values())[:MAX_CANDIDATES]


class RunDetectionService:
    """Reads a repository's manifests and proposes registry entries.

    Two sandbox round trips at most: one for the manifests at the detection root, and a second
    only when that root declares monorepo workspace globs whose packages must also be read.
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        runtime: SandboxRuntime | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.runtime = runtime or get_sandbox_runtime()

    async def detect(
        self,
        workspace_id: UUID,
        user_id: UUID,
        worktree_id: UUID | None = None,
    ) -> list[DetectedCommand]:
        workspace = await self._get_workspace(workspace_id, user_id)
        if not workspace.sandbox_id:
            raise RunDetectionUnavailableError("Workspace sandbox is not ready")

        checkout, _, _ = await resolve_worktree(self.session, workspace, worktree_id)
        app_root = validate_relative_subpath(getattr(workspace, "app_root", None))
        # An app root narrows detection to that package, which is what makes a monorepo
        # propose only the project the workspace is actually pointed at.
        root = join_subpath(checkout, app_root)

        files = await self._read_files(workspace, root, ROOT_MANIFESTS)
        package_dirs: tuple[str, ...] = ()
        if app_root is None:
            globs = _workspace_globs(_Manifests(files))
            if globs:
                package_dirs, extra = await self._read_packages(workspace, root, globs)
                files.update(extra)

        candidates = candidates_from_manifests(files, package_dirs=package_dirs)
        logger.info(
            "run_detection.completed",
            workspace_id=str(workspace_id),
            candidates=len(candidates),
            package_dirs=len(package_dirs),
        )
        return candidates

    async def read_proxy_configs(
        self,
        workspace_id: UUID,
        user_id: UUID,
        *,
        worktree_id: UUID | None,
        service_cwds: dict[str, str | None],
    ) -> dict[str, list[ProxyConfig]]:
        """Epic 13: dev-proxy configuration, scoped to each service's own working directory --
        never the whole `ROOT_MANIFESTS` bundle, which would inflate `/commands/detect`'s
        latency-sensitive round trip for a feature this endpoint doesn't need. At most two
        sandbox execs total regardless of how many services a workspace has: one for every
        directory's fixed-name config files, and a second only when at least one of them is an
        `angular.json` naming a `proxyConfig` file to also read.

        `service_cwds` maps an opaque caller-chosen key (typically the service id) to that
        service's `cwd` (relative to the resolved app root; `None` = the app root itself).
        """
        workspace = await self._get_workspace(workspace_id, user_id)
        if not workspace.sandbox_id:
            raise RunDetectionUnavailableError("Workspace sandbox is not ready")

        checkout, _, _ = await resolve_worktree(self.session, workspace, worktree_id)
        app_root = validate_relative_subpath(getattr(workspace, "app_root", None))
        root = join_subpath(checkout, app_root)

        directories: dict[str, str] = {
            key: (validate_relative_subpath(cwd) or "") for key, cwd in service_cwds.items()
        }
        unique_dirs = sorted(set(directories.values()))

        def _prefixed(directory: str, name: str) -> str:
            return f"{directory}/{name}" if directory else name

        wanted = [
            _prefixed(directory, name) for directory in unique_dirs for name in PROXY_CONFIG_FILES
        ]
        files = await self._read_files(workspace, root, tuple(wanted))

        per_directory: dict[str, dict[str, str]] = {directory: {} for directory in unique_dirs}
        for directory in unique_dirs:
            for name in PROXY_CONFIG_FILES:
                full_path = _prefixed(directory, name)
                if full_path in files:
                    per_directory[directory][name] = files[full_path]

        # Second pass: any angular.json declaring a proxyConfig -> read that one extra file.
        angular_wanted: list[str] = []
        angular_owner: dict[str, str] = {}
        for directory, file_map in per_directory.items():
            angular_json = file_map.get("angular.json")
            if angular_json is None:
                continue
            proxy_path = _angular_proxy_config_path(angular_json)
            if proxy_path:
                full_path = _prefixed(directory, proxy_path)
                angular_wanted.append(full_path)
                angular_owner[full_path] = directory

        angular_files_by_directory: dict[str, dict[str, str]] = {
            directory: {} for directory in unique_dirs
        }
        if angular_wanted:
            angular_files = await self._read_files(workspace, root, tuple(angular_wanted))
            for full_path, directory in angular_owner.items():
                content = angular_files.get(full_path)
                if content is not None:
                    proxy_path = full_path[len(f"{directory}/") :] if directory else full_path
                    angular_files_by_directory[directory][proxy_path] = content

        results: dict[str, list[ProxyConfig]] = {}
        for key, directory in directories.items():
            results[key] = proxy_configs_from_manifests(
                per_directory[directory],
                angular_proxy_files=angular_files_by_directory[directory],
            )
        return results

    async def _read_files(
        self, workspace: Workspace, root: str, paths: tuple[str, ...]
    ) -> dict[str, str]:
        """Read a fixed set of files in a single round trip, each bounded in size."""
        script = "".join(
            f'if [ -f {shlex.quote(path)} ]; then '
            f'echo "{_DELIMITER}{path}>>>"; '
            f"head -c {MAX_MANIFEST_BYTES} {shlex.quote(path)}; echo; fi\n"
            for path in paths
        )
        return self._parse_bundle(await self._exec(workspace, script, cwd=root))

    async def _read_packages(
        self, workspace: Workspace, root: str, globs: list[str]
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Expand monorepo globs and read each package's manifests."""
        # Deliberately *unquoted*: the shell has to expand the `*`. `_workspace_globs` only lets
        # through patterns matching `_SAFE_WORKSPACE_GLOB`, so nothing here can break out of the
        # word or start a second command.
        patterns = " ".join(f"{glob.rstrip('/')}/package.json" for glob in globs[:20])
        # `ls -d` on an unmatched glob is a normal miss, not a failure worth surfacing.
        listing = await self._exec(workspace, f"ls -d {patterns} 2>/dev/null || true", cwd=root)
        manifest_paths = [
            line.strip()
            for line in listing.splitlines()
            if line.strip().endswith("package.json") and ".." not in line
        ][:MAX_WORKSPACE_PACKAGES]
        if not manifest_paths:
            return (), {}

        directories = tuple(path.rsplit("/package.json", 1)[0] for path in manifest_paths)
        wanted: list[str] = []
        for directory in directories:
            wanted += [
                f"{directory}/package.json",
                f"{directory}/pnpm-lock.yaml",
                f"{directory}/yarn.lock",
                f"{directory}/package-lock.json",
                f"{directory}/pyproject.toml",
                f"{directory}/uv.lock",
                f"{directory}/poetry.lock",
                f"{directory}/requirements.txt",
                f"{directory}/manage.py",
                f"{directory}/alembic.ini",
            ]
        return directories, await self._read_files(workspace, root, tuple(wanted))

    async def _exec(self, workspace: Workspace, script: str, *, cwd: str) -> str:
        try:
            result = await self.runtime.exec(
                workspace,
                script,
                cwd=cwd,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except SandboxRuntimeError as error:
            raise RunDetectionUnavailableError(str(error)) from error
        return result.stdout or ""

    @staticmethod
    def _parse_bundle(output: str) -> dict[str, str]:
        """Split the delimited concatenation back into ``{path: content}``."""
        files: dict[str, str] = {}
        current: str | None = None
        buffer: list[str] = []
        for line in output.splitlines():
            if line.startswith(_DELIMITER) and line.endswith(">>>"):
                if current is not None:
                    files[current] = "\n".join(buffer)
                current = line[len(_DELIMITER) : -3]
                buffer = []
                continue
            if current is not None:
                buffer.append(line)
        if current is not None:
            files[current] = "\n".join(buffer)
        return files

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        return workspace
