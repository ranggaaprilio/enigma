from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models import Workspace
from app.services.access import (
    WorkspaceAccessDeniedError,
    WorkspaceRole,
    can_drive,
    require_workspace_access,
    resolve_workspace_role,
)
from app.services.workspaces import WorkspaceNotFoundError


class StubResult:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> str | None:
        return self.value


class StubSession:
    """Fakes the single `TeamMember.role` lookup `resolve_workspace_role` issues.

    `get` fakes the workspace fetch used by `require_workspace_access`.
    """

    def __init__(self, team_role: str | None, *, workspace: Workspace | None = None) -> None:
        self.team_role = team_role
        self.workspace = workspace

    async def execute(self, _statement):
        return StubResult(self.team_role)

    async def get(self, _model, _id):
        return self.workspace


def make_workspace(*, owner_id=None, team_id=None) -> Workspace:
    now = datetime.now(UTC)
    return Workspace(
        id=uuid4(),
        owner_id=owner_id or uuid4(),
        team_id=team_id,
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        sandbox_id="sandbox-demo",
        snapshot=None,
        workspace_path="workspace",
        status="ready",
        created_at=now,
        updated_at=now,
    )


# --- can_drive -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [WorkspaceRole.OWNER, WorkspaceRole.TEAM_LEAD, WorkspaceRole.TEAM_MEMBER],
)
def test_can_drive_allows_owner_lead_and_member(role):
    assert can_drive(role) is True


def test_can_drive_denies_team_viewer():
    assert can_drive(WorkspaceRole.TEAM_VIEWER) is False


# --- resolve_workspace_role --------------------------------------------------------------


async def test_resolve_workspace_role_is_owner_for_the_workspace_owner():
    owner_id = uuid4()
    workspace = make_workspace(owner_id=owner_id)

    role = await resolve_workspace_role(StubSession(None), workspace, owner_id)

    assert role == WorkspaceRole.OWNER


async def test_resolve_workspace_role_maps_team_roles():
    workspace = make_workspace(team_id=uuid4())

    lead = await resolve_workspace_role(StubSession("lead"), workspace, uuid4())
    member = await resolve_workspace_role(StubSession("member"), workspace, uuid4())
    viewer = await resolve_workspace_role(StubSession("viewer"), workspace, uuid4())

    assert lead == WorkspaceRole.TEAM_LEAD
    assert member == WorkspaceRole.TEAM_MEMBER
    assert viewer == WorkspaceRole.TEAM_VIEWER


async def test_resolve_workspace_role_is_none_without_team_membership():
    workspace = make_workspace(team_id=uuid4())

    role = await resolve_workspace_role(StubSession(None), workspace, uuid4())

    assert role is None


async def test_resolve_workspace_role_is_none_when_workspace_has_no_team_and_user_is_not_owner():
    workspace = make_workspace(team_id=None)

    role = await resolve_workspace_role(StubSession("lead"), workspace, uuid4())

    assert role is None


# --- require_workspace_access -------------------------------------------------------------


async def test_require_workspace_access_raises_not_found_for_missing_workspace():
    session = StubSession(None, workspace=None)

    with pytest.raises(WorkspaceNotFoundError):
        await require_workspace_access(session, uuid4(), uuid4())


async def test_require_workspace_access_raises_not_found_for_a_stranger():
    workspace = make_workspace(team_id=uuid4())
    session = StubSession(None, workspace=workspace)

    with pytest.raises(WorkspaceNotFoundError):
        await require_workspace_access(session, workspace.id, uuid4())


async def test_require_workspace_access_allows_a_team_viewer_when_drive_is_not_required():
    workspace = make_workspace(team_id=uuid4())
    session = StubSession("viewer", workspace=workspace)

    resolved, role = await require_workspace_access(session, workspace.id, uuid4())

    assert resolved is workspace
    assert role == WorkspaceRole.TEAM_VIEWER


async def test_require_workspace_access_denies_a_team_viewer_when_drive_is_required():
    workspace = make_workspace(team_id=uuid4())
    session = StubSession("viewer", workspace=workspace)

    with pytest.raises(WorkspaceAccessDeniedError):
        await require_workspace_access(session, workspace.id, uuid4(), require_drive=True)


async def test_require_workspace_access_allows_a_team_member_to_drive():
    workspace = make_workspace(team_id=uuid4())
    session = StubSession("member", workspace=workspace)

    _resolved, role = await require_workspace_access(
        session, workspace.id, uuid4(), require_drive=True
    )

    assert role == WorkspaceRole.TEAM_MEMBER


async def test_require_workspace_access_allows_the_owner_to_drive_regardless_of_team_lookup():
    owner_id = uuid4()
    workspace = make_workspace(owner_id=owner_id, team_id=uuid4())
    session = StubSession("viewer", workspace=workspace)

    _resolved, role = await require_workspace_access(
        session, workspace.id, owner_id, require_drive=True
    )

    assert role == WorkspaceRole.OWNER
