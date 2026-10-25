"""Detection proposes registry entries by *parsing* manifests, never by running them."""

import json

import pytest

from app.services.run_detection import (
    RunDetectionService,
    RunDetectionUnavailableError,
    candidates_from_manifests,
    infer_port,
    infer_role,
)
from app.services.run_presets import PRESETS_BY_SLUG, RUN_PRESETS, UnknownPresetError, get_preset


def _by_name(candidates, name):
    return next((candidate for candidate in candidates if candidate.name == name), None)


# --- Vite + FastAPI: the epic's headline acceptance criterion -------------


VITE_PACKAGE_JSON = json.dumps(
    {
        "name": "web",
        "scripts": {"dev": "vite --host 0.0.0.0", "build": "vite build", "test": "vitest run"},
    }
)

FASTAPI_PYPROJECT = """
[project]
name = "api"
dependencies = ["fastapi>=0.110", "uvicorn[standard]>=0.29"]

[dependency-groups]
dev = ["pytest>=8"]
"""


def test_detects_vite_web_and_fastapi_api_with_their_ports_and_directories():
    candidates = candidates_from_manifests(
        {
            "package.json": json.dumps({"workspaces": ["frontend", "backend"]}),
            "frontend/package.json": VITE_PACKAGE_JSON,
            "frontend/pnpm-lock.yaml": "lockfileVersion: '9.0'",
            "backend/pyproject.toml": FASTAPI_PYPROJECT,
        },
        package_dirs=("frontend", "backend"),
    )

    web = _by_name(candidates, "dev")
    assert web is not None
    assert web.cwd == "frontend"
    assert web.port == 5173
    assert web.command == "pnpm run dev"
    assert web.source_file == "frontend/package.json"

    api = _by_name(candidates, "dev:api")
    assert api is not None
    assert api.cwd == "backend"
    assert api.port == 8000
    assert "uvicorn" in api.command
    # The UI explains *why* each candidate was proposed.
    assert api.source_file == "backend/pyproject.toml"
    assert "FastAPI" in api.reason


def test_detects_an_install_command_per_package_manager():
    candidates = candidates_from_manifests(
        {"package.json": VITE_PACKAGE_JSON, "yarn.lock": "# yarn"},
    )

    install = _by_name(candidates, "install")
    assert install is not None
    assert install.command == "yarn install"
    assert install.role == "install"


def test_build_scripts_are_not_proposed_as_runnable():
    candidates = candidates_from_manifests({"package.json": VITE_PACKAGE_JSON})

    assert _by_name(candidates, "build") is None
    assert _by_name(candidates, "test") is not None


# --- app_root narrows detection to one package ---------------------------


def test_app_root_scoping_proposes_only_that_package():
    """With app_root set, the service reads only that directory, so nothing else is proposed."""
    candidates = candidates_from_manifests(
        {"package.json": VITE_PACKAGE_JSON, "pnpm-lock.yaml": "lockfileVersion: '9.0'"},
        package_dirs=(),
    )

    assert {candidate.cwd for candidate in candidates} == {None}
    assert _by_name(candidates, "dev") is not None


# --- mise is parsed, not executed ----------------------------------------


def test_mise_tasks_are_read_from_the_config_not_by_running_mise():
    candidates = candidates_from_manifests(
        {
            "mise.toml": (
                '[tools]\nnode = "20"\n\n'
                '[tasks.install]\nrun = "pnpm install"\n\n'
                '[tasks.dev]\nrun = "pnpm dev --port 4321"\n'
            )
        }
    )

    dev = _by_name(candidates, "dev")
    assert dev is not None
    assert dev.command == "mise run dev"
    assert dev.role == "dev"
    # The port comes from the task body, which was parsed out of the TOML.
    assert dev.port == 4321
    assert _by_name(candidates, "install").role == "install"


# --- a Makefile is scanned for target names, never evaluated -------------


def test_makefile_targets_are_parsed_and_recipes_ignored():
    candidates = candidates_from_manifests(
        {
            "Makefile": (
                ".PHONY: dev test\n"
                "CC = gcc\n"
                "dev:\n"
                "\t@echo running; rm -rf /tmp/should-never-run\n"
                "test: dev\n"
                "\tpytest\n"
            )
        }
    )

    names = {candidate.name for candidate in candidates}
    assert names == {"dev", "test"}
    # Only `make <target>` is ever proposed — the recipe body is not interpreted.
    assert {candidate.command for candidate in candidates} == {"make dev", "make test"}
    assert all(".PHONY" not in candidate.name for candidate in candidates)


def test_unparsable_manifests_are_skipped_rather_than_failing_detection():
    candidates = candidates_from_manifests(
        {"package.json": "{ this is not json", "Makefile": "dev:\n\techo hi\n"}
    )

    assert _by_name(candidates, "dev").command == "make dev"


# --- other ecosystems -----------------------------------------------------


@pytest.mark.parametrize(
    ("manifest", "expected_command", "expected_port"),
    [
        ("go.mod", "go run ./...", None),
        ("Cargo.toml", "cargo run", None),
        ("pom.xml", "./mvnw spring-boot:run", 8080),
        ("build.gradle", "./gradlew bootRun", 8080),
    ],
)
def test_detects_other_ecosystems(manifest, expected_command, expected_port):
    candidates = candidates_from_manifests({manifest: "content"})

    dev = _by_name(candidates, "dev")
    assert dev.command == expected_command
    assert dev.port == expected_port


def test_detects_django_and_alembic_migrations():
    django = candidates_from_manifests({"manage.py": "#!/usr/bin/env python"})
    alembic = candidates_from_manifests({"alembic.ini": "[alembic]"})

    assert _by_name(django, "migrate").command == "python manage.py migrate"
    assert _by_name(django, "dev").port == 8000
    assert _by_name(alembic, "migrate").command == "alembic upgrade head"


# --- inference helpers ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("dev", "dev"),
        ("dev:api", "dev"),
        ("start", "dev"),
        ("test:unit", "test"),
        ("lint-fix", "lint"),
        ("db:migrate", "migrate"),
        ("seed", "seed"),
        ("release", "custom"),
    ],
)
def test_infer_role(name, expected):
    assert infer_role(name) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("vite --host", 5173),
        ("next dev", 3000),
        ("uvicorn app.main:app", 8000),
        ("uvicorn app.main:app --port 9001", 9001),
        ("pnpm dev -p 4000", 4000),
        ("echo hello", None),
        ("serve --port 99999", None),
    ],
)
def test_infer_port(command, expected):
    assert infer_port(command) == expected


# --- the delimited bundle reader -----------------------------------------


def test_parse_bundle_splits_files_and_ignores_noise():
    output = (
        "leading noise that belongs to no file\n"
        "<<<ENIGMA-FILE:package.json>>>\n"
        '{"name": "demo"}\n'
        "<<<ENIGMA-FILE:Makefile>>>\n"
        "dev:\n"
        "\techo hi\n"
    )

    files = RunDetectionService._parse_bundle(output)

    assert json.loads(files["package.json"]) == {"name": "demo"}
    assert files["Makefile"].startswith("dev:")


# --- presets --------------------------------------------------------------


def test_every_preset_is_well_formed():
    valid_roles = {"install", "migrate", "dev", "test", "lint", "seed", "custom"}
    assert {preset.slug for preset in RUN_PRESETS} == set(PRESETS_BY_SLUG)
    for preset in RUN_PRESETS:
        assert preset.commands, f"{preset.slug} ships no commands"
        for command in preset.commands:
            assert command.role in valid_roles
            assert command.port is None or 1 <= command.port <= 65535


def test_vite_fastapi_preset_covers_both_services():
    preset = get_preset("vite+fastapi")
    ports = {command.port for command in preset.commands if command.port}

    assert ports == {5173, 8000}
    assert {command.cwd for command in preset.commands if command.cwd} == {"frontend", "backend"}


def test_unknown_preset_raises():
    with pytest.raises(UnknownPresetError):
        get_preset("does-not-exist")


# --- monorepos: packages inherit the root lockfile, globs actually expand ------------


PNPM_WORKSPACE_YAML = "packages:\n  - 'packages/*'\n  - 'apps/*'\n"


def test_workspace_packages_inherit_the_root_package_manager():
    """A pnpm workspace has one lockfile at the root; proposing `npm install` inside
    packages/web would write a stray package-lock.json and a second node_modules."""
    candidates = candidates_from_manifests(
        {
            "package.json": json.dumps({"name": "root", "private": True}),
            "pnpm-lock.yaml": "lockfileVersion: 9",
            "pnpm-workspace.yaml": PNPM_WORKSPACE_YAML,
            "packages/web/package.json": VITE_PACKAGE_JSON,
        },
        package_dirs=("packages/web",),
    )

    web = [candidate for candidate in candidates if candidate.cwd == "packages/web"]
    install = next(candidate for candidate in web if candidate.name == "install")
    assert install.command == "pnpm install"
    assert "workspace lockfile" in install.reason
    dev = next(candidate for candidate in web if candidate.name == "dev")
    assert dev.command == "pnpm run dev"


def test_a_packages_own_lockfile_beats_the_root_one():
    candidates = candidates_from_manifests(
        {
            "package.json": json.dumps({"name": "root"}),
            "pnpm-lock.yaml": "lockfileVersion: 9",
            "packages/legacy/package.json": VITE_PACKAGE_JSON,
            "packages/legacy/yarn.lock": "# yarn",
        },
        package_dirs=("packages/legacy",),
    )

    install = next(
        candidate
        for candidate in candidates
        if candidate.cwd == "packages/legacy" and candidate.name == "install"
    )
    assert install.command == "yarn install"


def test_root_without_a_lockfile_still_defaults_to_npm():
    candidates = candidates_from_manifests({"package.json": VITE_PACKAGE_JSON})

    install = _by_name(candidates, "install")
    assert install.command == "npm install"
    assert "no lockfile" in install.reason


class _ListingRuntime:
    """Records the scripts detection runs and answers the `ls -d` package listing."""

    def __init__(self, listing: str) -> None:
        self.listing = listing
        self.scripts: list[str] = []

    async def exec(self, workspace, script, *, cwd=None, env=None, timeout=None):
        from app.services.sandbox import RuntimeCommandResult

        self.scripts.append(script)
        if script.startswith("ls -d"):
            return RuntimeCommandResult(exit_code=0, stdout=self.listing, stderr="")
        return RuntimeCommandResult(exit_code=0, stdout="", stderr="")


async def test_read_packages_hands_globs_to_the_shell_unquoted_so_they_expand():
    """`ls -d 'packages/*/package.json'` looks for a directory literally named `*` and finds
    nothing, so every monorepo silently produced root-only candidates."""
    from app.core.config import get_settings

    runtime = _ListingRuntime("packages/web/package.json\npackages/api/package.json\n")
    service = RunDetectionService(session=None, settings=get_settings(), runtime=runtime)  # type: ignore[arg-type]

    directories, _ = await service._read_packages(None, "workspace", ["packages/*", "apps/*"])  # type: ignore[arg-type]

    assert runtime.scripts[0] == (
        "ls -d packages/*/package.json apps/*/package.json 2>/dev/null || true"
    )
    assert directories == ("packages/web", "packages/api")
    # Each package's own lockfiles are then read so its package manager can be decided.
    assert "packages/web/pnpm-lock.yaml" in runtime.scripts[1]
    assert "packages/web/uv.lock" in runtime.scripts[1]


@pytest.mark.parametrize(
    "glob",
    ["!packages/private", "packages/*; rm -rf /", "$(whoami)/*", "/abs/*", "../*", "a b/*", ""],
)
def test_unsafe_workspace_globs_are_dropped_not_escaped(glob):
    from app.services.run_detection import _Manifests, _workspace_globs

    manifests = _Manifests(
        {"package.json": json.dumps({"workspaces": [glob, "packages/*", "@scope/apps-*"]})}
    )

    assert _workspace_globs(manifests) == ["packages/*", "@scope/apps-*"]


# --- Python installer: evidence of the tool, not a substring match ---------------------


def test_uvicorn_dependency_does_not_imply_uv():
    """`"uv" in json.dumps(pyproject)` matched `uvicorn`, sending pip projects to `uv sync`."""
    candidates = candidates_from_manifests({"pyproject.toml": FASTAPI_PYPROJECT})

    install = _by_name(candidates, "install")
    assert install.command == "pip install -e ."
    # The FastAPI dev server is still proposed.
    assert _by_name(candidates, "dev:api").command.startswith("uvicorn")


@pytest.mark.parametrize(
    ("extra_files", "pyproject_tail", "expected"),
    [
        ({"uv.lock": "version = 1"}, "", "uv sync"),
        ({}, "\n[tool.uv]\ndev-dependencies = []\n", "uv sync"),
        ({"poetry.lock": "# poetry"}, "", "poetry install"),
        ({}, '\n[tool.poetry]\nname = "api"\n', "poetry install"),
    ],
)
def test_python_installer_follows_the_lockfile_or_tool_table(extra_files, pyproject_tail, expected):
    candidates = candidates_from_manifests(
        {"pyproject.toml": FASTAPI_PYPROJECT + pyproject_tail, **extra_files}
    )

    assert _by_name(candidates, "install").command == expected


def test_python_installer_is_decided_per_package():
    candidates = candidates_from_manifests(
        {
            "package.json": json.dumps({"name": "root"}),
            "backend/pyproject.toml": FASTAPI_PYPROJECT,
            "backend/uv.lock": "version = 1",
        },
        package_dirs=("backend",),
    )

    install = next(
        candidate
        for candidate in candidates
        if candidate.cwd == "backend" and candidate.name == "install"
    )
    assert install.command == "uv sync"


# ---------------------------------------------------------------------------
# read_proxy_configs: the IO layer over proxy_detection.py -- at most 2 sandbox execs,
# scoped to each service's own working directory.
# ---------------------------------------------------------------------------


class _FakeWorkspace:
    def __init__(self, *, owner_id, sandbox_id="sandbox-1", app_root=None) -> None:
        self.id = "workspace-1"
        self.owner_id = owner_id
        self.sandbox_id = sandbox_id
        self.app_root = app_root
        self.workspace_path = "workspace"
        self.default_branch = "main"


class _FakeSession:
    def __init__(self, workspace) -> None:
        self.workspace = workspace

    async def get(self, model, pk):
        return self.workspace if pk == self.workspace.id else None


class _FakeProxyRuntime:
    """Answers `RunDetectionService._read_files`'s delimited-bundle script by looking up each
    requested path (relative to the exec's own `cwd`) in a flat `{path: content}` map -- so the
    test controls file existence/content without needing a real shell or filesystem."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.execs: list[str] = []

    async def exec(self, workspace, script, *, cwd=None, env=None, timeout=None):
        import re
        import shlex

        from app.services.sandbox import RuntimeCommandResult

        self.execs.append(script)
        quoted_paths = re.findall(r"\[ -f (\S+) \]", script)
        lines: list[str] = []
        for quoted in quoted_paths:
            path = shlex.split(quoted)[0]
            content = self.files.get(path)
            if content is None:
                continue
            lines.append(f"<<<ENIGMA-FILE:{path}>>>")
            lines.append(content)
        stdout = "\n".join(lines) + ("\n" if lines else "")
        return RuntimeCommandResult(exit_code=0, stdout=stdout, stderr="")


def _service(session, runtime):
    from app.core.config import get_settings

    return RunDetectionService(session=session, settings=get_settings(), runtime=runtime)


async def test_read_proxy_configs_scopes_to_each_services_own_directory():
    workspace = _FakeWorkspace(owner_id="user-1")
    session = _FakeSession(workspace)
    vite_content = (
        "export default defineConfig({ server: { proxy: { '/api': "
        "'http://localhost:8000' } } })\n"
    )
    runtime = _FakeProxyRuntime({"frontend/vite.config.ts": vite_content})
    service = _service(session, runtime)

    results = await service.read_proxy_configs(
        "workspace-1",
        "user-1",
        worktree_id=None,
        service_cwds={"web": "frontend", "api": "backend"},
    )

    assert results["web"][0].configured is True
    assert results["api"] == []  # nothing found under backend/


async def test_read_proxy_configs_costs_at_most_one_exec_with_no_angular():
    workspace = _FakeWorkspace(owner_id="user-1")
    session = _FakeSession(workspace)
    runtime = _FakeProxyRuntime({})
    service = _service(session, runtime)

    await service.read_proxy_configs(
        "workspace-1", "user-1", worktree_id=None, service_cwds={"web": None}
    )

    assert len(runtime.execs) == 1


async def test_read_proxy_configs_costs_exactly_two_execs_with_an_angular_proxy_config():
    workspace = _FakeWorkspace(owner_id="user-1")
    session = _FakeSession(workspace)
    angular_json = (
        '{"projects": {"app": {"architect": {"serve": {"options": '
        '{"proxyConfig": "proxy.conf.json"}}}}}}'
    )
    proxy_conf = '{"/api": {"target": "http://localhost:8000"}}'
    runtime = _FakeProxyRuntime(
        {"angular.json": angular_json, "proxy.conf.json": proxy_conf}
    )
    service = _service(session, runtime)

    results = await service.read_proxy_configs(
        "workspace-1", "user-1", worktree_id=None, service_cwds={"web": None}
    )

    assert len(runtime.execs) == 2
    assert results["web"][0].configured is True
    assert results["web"][0].targets == ("http://localhost:8000",)


async def test_read_proxy_configs_deduplicates_shared_directories():
    """Two services sharing the same cwd (e.g. install + dev in the same package) must not
    double the exec's file list."""
    workspace = _FakeWorkspace(owner_id="user-1")
    session = _FakeSession(workspace)
    runtime = _FakeProxyRuntime({})
    service = _service(session, runtime)

    await service.read_proxy_configs(
        "workspace-1",
        "user-1",
        worktree_id=None,
        service_cwds={"a": "frontend", "b": "frontend"},
    )

    # Exactly one [ -f ... ] check per unique (directory, filename) pair -- "frontend" was
    # requested for two services, so it must appear only once in the combined script.
    from app.services.proxy_detection import PROXY_CONFIG_FILES

    script = runtime.execs[0]
    assert script.count("[ -f ") == len(PROXY_CONFIG_FILES)


async def test_read_proxy_configs_raises_when_sandbox_not_ready():
    workspace = _FakeWorkspace(owner_id="user-1", sandbox_id=None)
    session = _FakeSession(workspace)
    runtime = _FakeProxyRuntime({})
    service = _service(session, runtime)

    with pytest.raises(RunDetectionUnavailableError):
        await service.read_proxy_configs(
            "workspace-1", "user-1", worktree_id=None, service_cwds={"web": None}
        )
