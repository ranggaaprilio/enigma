import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import (
    CustomCommand,
    PreviewSession,
    User,
    Workspace,
    WorkspaceEnvVar,
    WorkspaceService,
    WorkspaceWorktree,
)
from app.services.sandbox import RuntimeCommandResult, SandboxRuntimeError
from app.services.service_scripts import (
    build_mise_run_script,
    build_mise_setup_script,
    build_registry_script,
    fresh_log_line,
    service_paths,
)
from app.services.workspace_services import (
    WorkspaceServiceLimitError,
    WorkspaceServiceManager,
    WorkspaceServiceNotFoundError,
    WorkspaceServicePortConflictError,
)
from app.services.workspaces import WorkspaceSecretCipher


class FakeFileInfo:
    def __init__(self, is_dir: bool) -> None:
        self.is_dir = is_dir


class FakeFs:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing

    async def get_file_info(self, path: str) -> FakeFileInfo:
        if path not in self.existing:
            raise FileNotFoundError(path)
        return FakeFileInfo(is_dir=path.endswith("/.git"))


class FakeSandbox:
    def __init__(self, existing: set[str]) -> None:
        self.fs = FakeFs(existing)


class FakeRuntime:
    """Per-row controllable fake: `statuses`/`pids_alive` are keyed by the row's *string* id,
    so a test can drive several services independently in one probe call."""

    def __init__(
        self,
        sandbox: FakeSandbox,
        *,
        statuses: dict[str, str] | None = None,
        pids_alive: set[str] | None = None,
        open_ports: set[int] | None = None,
        log_content: str = "",
        fail_probe: bool = False,
    ) -> None:
        self.sandbox = sandbox
        self.statuses = statuses or {}
        self.pids_alive = pids_alive or set()
        self.open_ports = open_ports or set()
        self.log_content = log_content
        # Simulates a transient sandbox exec failure on the combined probe call, so a test
        # can verify `ensure_running` never relaunches on "no evidence" alone.
        self.fail_probe = fail_probe
        self.commands: list[str] = []
        self.cwds: list[str | None] = []
        self.envs: list[dict | None] = []
        self.exposed_ports: list[int] = []
        self.fail_expose = False

    async def expose_port(self, workspace, port) -> None:
        if self.fail_expose:
            raise SandboxRuntimeError("sandbox unreachable")
        self.exposed_ports.append(port)

    async def fs(self, workspace) -> FakeFs:
        return self.sandbox.fs

    async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
        self.commands.append(command)
        self.cwds.append(cwd)
        self.envs.append(env)
        if "ENIGMA_PROBE_EOF" in command:
            if self.fail_probe:
                raise SandboxRuntimeError("sandbox unreachable")
            lines = []
            for row_id, port_token in re.findall(
                r"probe\('([^']*)', '[^']*', '[^']*', '[^']*', "
                r"(None|\d+), (?:None|'[^']*')\)",
                command,
            ):
                status = self.statuses.get(row_id, "none")
                pid_alive = 1 if row_id in self.pids_alive else 0
                port_open = 0
                if port_token != "None":
                    port_open = 1 if int(port_token) in self.open_ports else 0
                lines.append(f"{row_id} {status} {pid_alive} {port_open}")
            stdout = ("\n".join(lines) + "\n") if lines else ""
            return RuntimeCommandResult(exit_code=0, stdout=stdout, stderr="")
        if command.startswith("tail -c"):
            return RuntimeCommandResult(exit_code=0, stdout=self.log_content, stderr="")
        if "ENIGMA_PORTS_EOF" in command:
            match = re.search(r"ports = \[([^\]]*)\]", command)
            candidates = (
                [int(tok) for tok in match.group(1).split(",") if tok.strip()] if match else []
            )
            matched = [port for port in candidates if port in self.open_ports]
            return RuntimeCommandResult(
                exit_code=0, stdout=" ".join(str(p) for p in matched) + "\n", stderr=""
            )
        return RuntimeCommandResult(exit_code=0, stdout="", stderr="")


@pytest.fixture
async def db_session():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    async with engine.connect() as conn:
        await conn.begin()
        session_maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with session_maker() as session:
            await session.begin_nested()
            yield session
            await session.rollback()
    await engine.dispose()


async def make_workspace(session, *, app_root: str | None = None) -> tuple[User, Workspace]:
    user = User(
        id=uuid4(),
        email=f"owner-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace = Workspace(
        id=uuid4(),
        owner_id=user.id,
        name="Demo",
        slug=f"demo-{uuid4().hex[:8]}",
        repo_url="https://github.com/example/demo.git",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        app_root=app_root,
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add_all([user, workspace])
    await session.flush()
    return user, workspace


async def make_service_command(
    session,
    workspace: Workspace,
    *,
    name: str = "API",
    role: str = "custom",
    command: str = "uvicorn app.main:app --port 8000",
    port: int | None = 8000,
    enabled: bool = True,
) -> CustomCommand:
    now = datetime.now(UTC)
    cmd = CustomCommand(
        id=uuid4(),
        workspace_id=workspace.id,
        name=name,
        command=command,
        role=role,
        kind="service",
        cwd=None,
        port=port,
        enabled=enabled,
        sort_order=0,
        requires_confirmation=False,
        created_at=now,
        updated_at=now,
    )
    session.add(cmd)
    await session.flush()
    return cmd


async def _row_for_command(session, workspace_id, command_id) -> WorkspaceService:
    result = await session.execute(
        select(WorkspaceService).where(
            WorkspaceService.workspace_id == workspace_id,
            WorkspaceService.custom_command_id == command_id,
        )
    )
    row = result.scalars().first()
    assert row is not None
    return row


# --- sync / list -----------------------------------------------------------------


async def test_list_creates_one_row_per_enabled_service_command(db_session):
    user, workspace = await make_workspace(db_session)
    await make_service_command(db_session, workspace, name="API", port=8000)
    await make_service_command(db_session, workspace, name="Disabled", port=9999, enabled=False)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)

    rows, primary = await manager.list(workspace.id, user.id)

    # Enabled service command -> one row; disabled command -> none; no dev-role command ->
    # a built-in row is present too (since no enabled dev-role service exists).
    names = {row.custom_command_id for row in rows}
    assert len(rows) == 2
    assert None in names  # the built-in row
    assert primary.custom_command_id is None  # no dev-role command -> built-in is primary


async def test_list_has_no_builtin_row_once_a_dev_service_exists(db_session):
    user, workspace = await make_workspace(db_session)
    dev_cmd = await make_service_command(
        db_session, workspace, name="Dev", role="dev", command="pnpm dev", port=5173
    )
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)

    rows, primary = await manager.list(workspace.id, user.id)

    assert len(rows) == 1
    assert rows[0].custom_command_id == dev_cmd.id
    assert primary.id == rows[0].id


# --- start -------------------------------------------------------------------


async def test_start_records_declared_port_and_marks_desired(db_session):
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)

    started, warning = await manager.start(workspace.id, row.id, user.id)

    assert warning is None
    assert started.desired is True
    assert started.port == 8000
    assert started.status == "installing"
    launch = next(c for c in runtime.commands if "nohup" in c)
    assert "uvicorn app.main:app --port 8000" in launch


async def test_start_infers_port_from_command_text_when_undeclared(db_session):
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(
        db_session, workspace, command="pnpm dev --port 5173", port=None
    )
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)

    started, _warning = await manager.start(workspace.id, row.id, user.id)

    assert started.port == 5173


async def test_start_conflicts_on_a_port_another_running_row_already_owns(db_session):
    user, workspace = await make_workspace(db_session)
    web = await make_service_command(
        db_session, workspace, name="Web", command="pnpm dev", port=5173
    )
    api = await make_service_command(
        db_session, workspace, name="API", command="uvicorn app.main:app", port=5173
    )
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    web_row = await _row_for_command(db_session, workspace.id, web.id)
    api_row = await _row_for_command(db_session, workspace.id, api.id)

    await manager.start(workspace.id, web_row.id, user.id)
    with pytest.raises(WorkspaceServicePortConflictError):
        await manager.start(workspace.id, api_row.id, user.id)


async def test_start_enforces_the_running_service_cap(db_session):
    user, workspace = await make_workspace(db_session)
    settings = get_settings()
    settings.max_running_services_per_workspace = 1
    cmd1 = await make_service_command(db_session, workspace, name="One", port=8001)
    cmd2 = await make_service_command(db_session, workspace, name="Two", port=8002)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    manager = WorkspaceServiceManager(db_session, settings, runtime)
    await manager.list(workspace.id, user.id)
    row1 = await _row_for_command(db_session, workspace.id, cmd1.id)
    row2 = await _row_for_command(db_session, workspace.id, cmd2.id)

    await manager.start(workspace.id, row1.id, user.id)
    with pytest.raises(WorkspaceServiceLimitError):
        await manager.start(workspace.id, row2.id, user.id)


async def test_start_unknown_service_id_raises_not_found(db_session):
    user, workspace = await make_workspace(db_session)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)

    with pytest.raises(WorkspaceServiceNotFoundError):
        await manager.start(workspace.id, uuid4(), user.id)


# --- stop / restart -----------------------------------------------------------


async def test_stop_clears_desired_and_stops_its_preview_session(db_session):
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}), open_ports={8000})
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)
    await manager.start(workspace.id, row.id, user.id)

    # Mark it running (as a probe following the launch would) so a preview session exists.
    runtime.statuses[str(row.id)] = "running"
    runtime.pids_alive.add(str(row.id))
    await manager.list(workspace.id, user.id)
    preview = (
        await db_session.execute(
            select(PreviewSession).where(PreviewSession.service_id == row.id)
        )
    ).scalars().first()
    assert preview is not None
    assert preview.status == "active"

    stopped = await manager.stop(workspace.id, row.id, user.id)

    assert stopped.desired is False
    assert stopped.status == "stopped"
    await db_session.refresh(preview)
    assert preview.status == "stopped"
    assert any("killtree" in c for c in runtime.commands)


async def _running_service_with_preview(db_session, runtime: FakeRuntime):
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)
    await manager.start(workspace.id, row.id, user.id)
    runtime.statuses[str(row.id)] = "running"
    runtime.pids_alive.add(str(row.id))
    await manager.list(workspace.id, user.id)
    preview = (
        await db_session.execute(
            select(PreviewSession).where(PreviewSession.service_id == row.id)
        )
    ).scalars().first()
    return preview


async def test_a_running_service_exposes_its_port_for_the_preview(db_session):
    """local-docker-runtime Epic 04: the Docker provider relays a loopback-only dev server
    onto the sandbox network when its preview session is (re)confirmed."""
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}), open_ports={8000})

    preview = await _running_service_with_preview(db_session, runtime)

    assert preview is not None and preview.status == "active"
    assert 8000 in runtime.exposed_ports


async def test_a_failed_expose_still_records_the_preview_session(db_session):
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}), open_ports={8000})
    runtime.fail_expose = True

    preview = await _running_service_with_preview(db_session, runtime)

    assert preview is not None and preview.status == "active"


async def test_restart_keeps_desired_and_targets_a_new_worktree(db_session):
    user, workspace = await make_workspace(db_session)
    worktree = WorkspaceWorktree(
        id=uuid4(),
        workspace_id=workspace.id,
        branch="feature-x",
        path="worktrees/feature-x",
        base_ref="main",
        is_base=False,
        status="active",
    )
    db_session.add(worktree)
    await db_session.flush()
    cmd = await make_service_command(db_session, workspace, port=8000)
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "worktrees/feature-x/.git"}), open_ports={8000}
    )
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)
    await manager.start(workspace.id, row.id, user.id)

    restarted = await manager.restart(workspace.id, row.id, user.id, worktree.id)

    assert restarted.desired is True
    assert restarted.worktree_id == worktree.id
    launch = runtime.commands[-1]
    launch_idx = runtime.commands.index(launch)
    assert runtime.cwds[launch_idx] == "worktrees/feature-x"


# --- ensure_running relaunch decisions ----------------------------------------


async def test_ensure_running_relaunches_a_dead_after_wake_row(db_session):
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}), open_ports={8000})
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)
    await manager.start(workspace.id, row.id, user.id)
    # Simulate a sandbox sleep/wake: the status file still says "running" but the pid is dead
    # (and its port has gone dark with it), with no relaunch backoff in the way.
    row.started_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()
    runtime.statuses[str(row.id)] = "running"
    runtime.open_ports.clear()
    # pid NOT in pids_alive -> dead.
    launches_before = sum(1 for c in runtime.commands if "nohup" in c)

    rows, _primary = await manager.ensure_running(workspace.id, user.id)

    relaunched = next(r for r in rows if r.id == row.id)
    assert relaunched.status == "installing"
    launches_after = sum(1 for c in runtime.commands if "nohup" in c)
    assert launches_after == launches_before + 1


async def test_ensure_running_never_relaunches_an_exited_service(db_session):
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)
    await manager.start(workspace.id, row.id, user.id)
    runtime.statuses[str(row.id)] = "exited:1"
    launches_before = sum(1 for c in runtime.commands if "nohup" in c)

    rows, _primary = await manager.ensure_running(workspace.id, user.id)

    relaunched = next(r for r in rows if r.id == row.id)
    assert relaunched.status == "exited"
    assert relaunched.exit_code == 1
    launches_after = sum(1 for c in runtime.commands if "nohup" in c)
    assert launches_after == launches_before


async def test_ensure_running_respects_the_relaunch_backoff(db_session):
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}), open_ports={8000})
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)
    await manager.start(workspace.id, row.id, user.id)
    # Launched moments ago -> inside the backoff window even though it now looks dead.
    row.started_at = datetime.now(UTC)
    await db_session.flush()
    runtime.statuses[str(row.id)] = "running"
    launches_before = sum(1 for c in runtime.commands if "nohup" in c)

    await manager.ensure_running(workspace.id, user.id)

    launches_after = sum(1 for c in runtime.commands if "nohup" in c)
    assert launches_after == launches_before


# --- worktree removal ----------------------------------------------------------


async def test_on_worktree_removed_stops_services_running_there(db_session):
    user, workspace = await make_workspace(db_session)
    worktree = WorkspaceWorktree(
        id=uuid4(),
        workspace_id=workspace.id,
        branch="feature-x",
        path="worktrees/feature-x",
        base_ref="main",
        is_base=False,
        status="active",
    )
    db_session.add(worktree)
    await db_session.flush()
    cmd = await make_service_command(db_session, workspace, port=8000)
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "worktrees/feature-x/.git"}), open_ports={8000}
    )
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)
    await manager.start(workspace.id, row.id, user.id, worktree.id)

    await manager.on_worktree_removed(workspace, worktree.id)

    await db_session.refresh(row)
    assert row.status == "stopped"
    assert row.desired is False
    assert row.last_error == "Worktree removed"


# --- logs ------------------------------------------------------------------


async def test_logs_tail_redacts_workspace_secrets(db_session):
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    cipher = WorkspaceSecretCipher(get_settings().workspace_env_encryption_key)
    secret_value = "sk-live-abcdef1234567890"
    db_session.add(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            key="API_KEY",
            encrypted_value=cipher.encrypt(secret_value),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git"}),
        log_content=f"connecting with API_KEY={secret_value}\n",
    )
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)

    content = await manager.logs_tail(workspace.id, row.id, user.id, 20_000)

    assert secret_value not in content
    assert "[redacted]" in content


async def test_logs_tail_redacts_service_scoped_workspace_secrets(db_session):
    """`load_all_values` must catch scoped (per-service) rows too, not just workspace-wide ones
    -- ``resolve()`` alone would miss a row scoped to a *different* service."""
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    cipher = WorkspaceSecretCipher(get_settings().workspace_env_encryption_key)
    secret_value = "svc-scoped-secret-1234567890"
    db_session.add(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            key="SERVICE_TOKEN",
            encrypted_value=cipher.encrypt(secret_value),
            scope="service",
            custom_command_id=cmd.id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git"}),
        log_content=f"connecting with SERVICE_TOKEN={secret_value}\n",
    )
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)

    content = await manager.logs_tail(workspace.id, row.id, user.id, 20_000)

    assert secret_value not in content
    assert "[redacted]" in content


async def test_launch_passes_env_to_runtime_exec(db_session):
    """Injection happens exclusively through the runtime's `env` parameter -- a secret value
    must never appear in the launched script body (`runtime.commands`)."""
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    cipher = WorkspaceSecretCipher(get_settings().workspace_env_encryption_key)
    secret_value = "sk-live-abcdef1234567890"
    db_session.add(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            key="API_KEY",
            encrypted_value=cipher.encrypt(secret_value),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)

    await manager.start(workspace.id, row.id, user.id)

    # Epic 13: every launch also carries ENIGMA_* exports (workspace id, branch, this row's own
    # name/port) -- assert the user's own value survives untouched rather than pinning the
    # exact dict.
    assert runtime.envs[-1]["API_KEY"] == secret_value
    assert all(secret_value not in command for command in runtime.commands)


async def test_launch_resolves_templates_but_never_leaks_the_resolved_value_into_the_script(
    db_session,
):
    """The same invariant as above, for a *resolved template* value rather than a plain
    secret: substitution feeds the runtime's `env` parameter only, never the script body."""
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    db_session.add(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            key="WORKSPACE_ID_ECHO",
            encrypted_value=WorkspaceSecretCipher(
                get_settings().workspace_env_encryption_key
            ).encrypt("${ENIGMA_WORKSPACE_ID}"),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)

    await manager.start(workspace.id, row.id, user.id)

    resolved_value = str(workspace.id)
    assert runtime.envs[-1]["WORKSPACE_ID_ECHO"] == resolved_value
    assert all(resolved_value not in command for command in runtime.commands)


async def test_ensure_running_does_not_relaunch_on_a_transient_probe_failure(db_session):
    """A sandbox exec failure is not evidence the process is dead — only that we couldn't
    check. Relaunching on that alone risks a duplicate process next to one still running."""
    user, workspace = await make_workspace(db_session)
    cmd = await make_service_command(db_session, workspace, port=8000)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}), open_ports={8000})
    manager = WorkspaceServiceManager(db_session, get_settings(), runtime)
    await manager.list(workspace.id, user.id)
    row = await _row_for_command(db_session, workspace.id, cmd.id)
    await manager.start(workspace.id, row.id, user.id)
    launches_before = sum(1 for c in runtime.commands if "nohup" in c)

    runtime.fail_probe = True
    rows, _primary = await manager.ensure_running(workspace.id, user.id)

    launches_after = sum(1 for c in runtime.commands if "nohup" in c)
    assert launches_after == launches_before
    # The row's last-known status is left alone rather than guessed at.
    relaunched = next(r for r in rows if r.id == row.id)
    assert relaunched.status == "installing"


@pytest.mark.parametrize(
    "build",
    [
        lambda paths: build_registry_script(
            paths, "gen", command="serve", install_command=None, serve=True
        ),
        lambda paths: build_mise_run_script(paths, "gen", install_deps=False),
        lambda paths: build_mise_setup_script(
            paths, "gen", mise_auto_install=False, mise_install_url="https://mise.run"
        ),
    ],
)
def test_every_launch_writes_a_new_log_file_so_the_live_stream_reads_it_from_the_top(
    build, tmp_path
) -> None:
    # Regression: a launch used to truncate the log in place, and the live stream's `tail -F`
    # kept its old offset whenever the new log had already grown past the old size -- the
    # restarted service's first lines (often all of them) never reached the socket.
    import subprocess

    paths = service_paths(uuid4())
    assert fresh_log_line(paths.log) in build(paths)

    log = tmp_path / "service.log"
    log.write_text("old output from the previous run\n" * 50)
    old_inode = log.stat().st_ino
    subprocess.run(["sh", "-c", f"{fresh_log_line(str(log))}echo new run"], check=True)
    assert log.read_text() == "new run\n"
    assert log.stat().st_ino != old_inode
