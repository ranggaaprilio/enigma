"""Plan 3 Task 2: where a Spec Kit workflow's verification criteria come from.

Precedence: the workflow's own criteria, else the workspace's first enabled ``test`` custom
command, else none (implement completes as before). Commands run in the feature worktree,
under the workspace's ``app_root`` and the command's own ``cwd``, like the Commands panel.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import CustomCommand, SpecWorkflow, User, Workspace
from app.services.verification.spec_link import criteria_for_workflow, plan_ref_for


@pytest.fixture
async def session():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    async with engine.connect() as conn:
        await conn.begin()
        maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with maker() as s:
            yield s
            await s.rollback()
    await engine.dispose()


async def _seed(session, *, app_root=None, worktree_path="worktrees/001-login", verification=None):
    user = User(id=uuid4(), email=f"{uuid4().hex}@example.com", password_hash="x", role="owner")
    workspace = Workspace(
        id=uuid4(),
        owner_id=user.id,
        name="W",
        slug=uuid4().hex[:10],
        default_branch="main",
        runtime="docker",
        status="ready",
        workspace_path="workspace",
        sandbox_id=uuid4().hex,
        app_root=app_root,
    )
    session.add(user)
    await session.flush()
    session.add(workspace)
    await session.flush()
    workflow = SpecWorkflow(
        workspace_id=workspace.id,
        user_id=user.id,
        feature_name="Login",
        feature_slug="001-login",
        description="d",
        branch="001-login",
        spec_dir="specs/001-login",
        worktree_path=worktree_path,
        verification=verification,
    )
    session.add(workflow)
    await session.flush()
    return workspace, workflow


def _command(workspace, *, role="test", command="pytest -q", cwd=None, enabled=True, order=10):
    return CustomCommand(
        workspace_id=workspace.id,
        name=f"c-{uuid4().hex[:6]}",
        command=command,
        role=role,
        cwd=cwd,
        enabled=enabled,
        sort_order=order,
    )


async def test_the_workflows_own_criteria_win(session) -> None:
    own = {"test_command": "make test", "observe_port": 5173, "max_iterations": 3}
    workspace, workflow = await _seed(session, verification=own)
    session.add(_command(workspace, command="pytest -q"))
    await session.flush()

    criteria = await criteria_for_workflow(session, workspace, workflow)

    assert criteria.test_command == "make test"
    assert criteria.observe_port == 5173
    assert criteria.max_iterations == 3
    assert criteria.cwd == "worktrees/001-login"


async def test_falls_back_to_the_first_enabled_test_command(session) -> None:
    workspace, workflow = await _seed(session, app_root="backend")
    session.add_all(
        [
            _command(workspace, command="disabled", enabled=False, order=1),
            _command(workspace, role="lint", command="ruff check .", order=2),
            _command(workspace, command="uv run pytest -q", cwd="tests", order=3),
            _command(workspace, command="later", order=9),
        ]
    )
    await session.flush()

    criteria = await criteria_for_workflow(session, workspace, workflow)

    assert criteria.test_command == "uv run pytest -q"
    assert criteria.cwd == "worktrees/001-login/backend/tests"
    assert criteria.max_iterations == 2
    assert criteria.observe_port is None


async def test_no_criteria_and_no_test_command_means_no_verification(session) -> None:
    workspace, workflow = await _seed(session)
    session.add(_command(workspace, command="disabled", enabled=False))
    await session.flush()

    assert await criteria_for_workflow(session, workspace, workflow) is None


async def test_runs_in_the_base_tree_before_the_worktree_exists(session) -> None:
    workspace, workflow = await _seed(
        session, worktree_path=None, verification={"test_command": "pytest", "max_iterations": 1}
    )

    criteria = await criteria_for_workflow(session, workspace, workflow)

    assert criteria.cwd == "workspace"


def test_plan_ref_points_at_the_features_plan() -> None:
    workflow = SpecWorkflow(spec_dir="specs/001-login")
    assert plan_ref_for(workflow) == "specs/001-login/plan.md"
