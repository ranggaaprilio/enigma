# ruff: noqa: F811  (db_session is an imported pytest fixture)
"""Plan 9 Task 3: opt-in auto-verify of completed agent runs.

With ``workspaces.auto_verify_agent_runs`` on, every agent run that completes is verified the way
the Verify action would, when the workspace has criteria. Never for a fix run, a Spec Kit phase
run, a run that is already verified or did not complete; and nothing about it can fail the run.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.models import AgentRun, SpecPhase, SpecWorkflow, VerificationRun
from app.services.verification import agent_link
from tests.test_verification_agent_link import _agent_run, _test_command
from tests.test_verification_api import db_session, make_user_workspace  # noqa: F401

SETTINGS = Settings(run_execution_mode="inline")


@pytest.fixture
def started(monkeypatch) -> list:
    calls: list = []

    async def fake_start(run_id, settings, **_kwargs):
        calls.append(run_id)

    monkeypatch.setattr(agent_link, "start_verification_run", fake_start)
    return calls


async def _workspace(session, *, auto=True, criteria=True):
    user, workspace = await make_user_workspace(session)
    workspace.auto_verify_agent_runs = auto
    session.add(workspace)
    if criteria:
        await _test_command(session, workspace)
    await session.flush()
    return user, workspace


async def _linked(session, agent_run) -> list[VerificationRun]:
    rows = await session.scalars(
        select(VerificationRun).where(VerificationRun.source_agent_run_id == agent_run.id)
    )
    return list(rows)


async def test_a_completed_run_is_verified_when_auto_verify_is_on(db_session, started):
    session, maker = db_session
    user, workspace = await _workspace(session)
    agent_run = await _agent_run(session, workspace, user)

    verification_id = await agent_link.auto_verify_agent_run(maker, SETTINGS, agent_run.id)

    [linked] = await _linked(session, agent_run)
    assert verification_id == linked.id
    assert linked.created_by == user.id  # attributed (and budgeted) to the run's author
    assert linked.criteria["test_command"] == "npm test"
    assert started == [linked.id]


async def test_nothing_happens_when_auto_verify_is_off(db_session, started):
    session, maker = db_session
    user, workspace = await _workspace(session, auto=False)
    agent_run = await _agent_run(session, workspace, user)

    assert await agent_link.auto_verify_agent_run(maker, SETTINGS, agent_run.id) is None
    assert await _linked(session, agent_run) == [] and started == []


@pytest.mark.parametrize("case", ["no-criteria", "error", "stopped", "fix", "spec", "verified"])
async def test_runs_that_must_not_be_auto_verified_are_skipped(db_session, started, case):
    session, maker = db_session
    user, workspace = await _workspace(session, criteria=case != "no-criteria")
    status = case if case in ("error", "stopped") else "completed"
    agent_run = await _agent_run(session, workspace, user, status=status)
    if case == "fix":
        session.add(
            VerificationRun(
                workspace_id=workspace.id,
                plan_ref="p",
                status="approved",
                agent_run_id=agent_run.id,
            )
        )
    elif case == "spec":
        workflow = SpecWorkflow(
            workspace_id=workspace.id,
            user_id=user.id,
            feature_name="F",
            feature_slug=f"f-{uuid4().hex[:6]}",
            description="d",
            branch="f",
            spec_dir="specs/f",
        )
        session.add(workflow)
        await session.flush()
        session.add(
            SpecPhase(workflow_id=workflow.id, phase="implement", agent_run_id=agent_run.id)
        )
    elif case == "verified":
        session.add(
            VerificationRun(
                workspace_id=workspace.id,
                plan_ref="p",
                status="approved",
                source_agent_run_id=agent_run.id,
            )
        )
    await session.flush()
    before = len(await _linked(session, agent_run))

    assert await agent_link.auto_verify_agent_run(maker, SETTINGS, agent_run.id) is None
    assert len(await _linked(session, agent_run)) == before
    assert started == []


async def test_a_workspace_already_verifying_is_left_alone(db_session, started):
    session, maker = db_session
    user, workspace = await _workspace(session)
    session.add(VerificationRun(workspace_id=workspace.id, plan_ref="busy", status="testing"))
    agent_run = await _agent_run(session, workspace, user)

    assert await agent_link.auto_verify_agent_run(maker, SETTINGS, agent_run.id) is None
    assert started == []


# --- the hook in AgentService ---------------------------------------------------------------


async def _finish(db_session, monkeypatch, *, status, failing=False):
    from app.services.agents import AgentService

    session, maker = db_session
    user, workspace = await _workspace(session)
    agent_run = await _agent_run(session, workspace, user, status=status)
    calls: list = []

    async def fake_auto_verify(session_maker, settings, run_id):
        calls.append(run_id)
        if failing:
            raise RuntimeError("queue down")

    monkeypatch.setattr(agent_link, "auto_verify_agent_run", fake_auto_verify)
    service = AgentService(session, maker, SETTINGS)
    await service._record_run_finished(session, agent_run)
    return agent_run, calls


async def test_a_finished_run_hands_itself_to_auto_verify(db_session, monkeypatch):
    agent_run, calls = await _finish(db_session, monkeypatch, status="completed")
    assert calls == [agent_run.id]


async def test_only_a_completed_run_is_handed_over(db_session, monkeypatch):
    _, calls = await _finish(db_session, monkeypatch, status="stopped")
    assert calls == []


async def test_auto_verify_can_never_fail_the_run(db_session, monkeypatch):
    agent_run, calls = await _finish(db_session, monkeypatch, status="completed", failing=True)
    assert calls == [agent_run.id]
    session, _ = db_session
    assert (await session.get(AgentRun, agent_run.id)).status == "completed"


# --- the workspace setting ------------------------------------------------------------------


async def test_the_owner_turns_auto_verify_on_and_off(db_session):
    from app.services.workspaces import SqlAlchemyWorkspaceService, WorkspaceSecretCipher

    session, _ = db_session
    user, workspace = await _workspace(session, auto=False)
    service = SqlAlchemyWorkspaceService(
        session=session,
        cipher=WorkspaceSecretCipher(SETTINGS.workspace_env_encryption_key),
        runtime=object(),  # type: ignore[arg-type]  (update never touches the sandbox)
    )

    updated = await service.update(workspace.id, user.id, auto_verify_agent_runs=True)
    assert updated.auto_verify_agent_runs is True
    # Not given = unchanged.
    assert (await service.update(workspace.id, user.id, name="Renamed")).auto_verify_agent_runs
    off = await service.update(workspace.id, user.id, auto_verify_agent_runs=False)
    assert off.auto_verify_agent_runs is False


def test_the_setting_is_part_of_the_workspace_api() -> None:
    from app.schemas.workspaces import WorkspaceResponse, WorkspaceUpdateRequest

    assert "auto_verify_agent_runs" in WorkspaceResponse.model_fields
    assert WorkspaceResponse.model_fields["auto_verify_agent_runs"].default is False
    request = WorkspaceUpdateRequest(auto_verify_agent_runs=True)
    assert request.auto_verify_agent_runs is True and "name" not in request.model_fields_set
