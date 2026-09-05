from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth as auth_module
from app.api import comments as comments_module
from app.core.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models import AgentRun, Notification, PullRequest, SpecPhase, SpecWorkflow, User, Workspace
from app.services.comments import CommentNotFoundError, CommentService, InvalidCommentTargetError
from app.services.workspaces import WorkspaceNotFoundError


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


async def override_get_current_user(user: User):
    async def _override() -> User:
        return user

    return _override


def comment_service_override(session):
    async def _override() -> CommentService:
        return CommentService(session)

    return _override


async def make_user(session, *, email: str) -> User:
    user = User(
        id=uuid4(),
        email=email,
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    return user


async def make_workspace(session, owner: User) -> Workspace:
    workspace = Workspace(
        id=uuid4(),
        owner_id=owner.id,
        name="Demo",
        slug=f"demo-{uuid4().hex[:8]}",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(workspace)
    await session.flush()
    return workspace


async def make_run(session, workspace: Workspace, user: User) -> AgentRun:
    run = AgentRun(
        workspace_id=workspace.id,
        user_id=user.id,
        prompt="Do the thing",
        status="completed",
        provider="anthropic",
        model="claude",
    )
    session.add(run)
    await session.flush()
    return run


async def make_pull_request(session, workspace: Workspace) -> PullRequest:
    pull_request = PullRequest(
        workspace_id=workspace.id,
        number=1,
        url="https://github.com/example/demo/pull/1",
        state="open",
        head="feature",
        base="main",
        title="Add feature",
    )
    session.add(pull_request)
    await session.flush()
    return pull_request


async def make_spec_phase(session, workspace: Workspace, user: User) -> SpecPhase:
    workflow = SpecWorkflow(
        workspace_id=workspace.id,
        user_id=user.id,
        feature_name="Add auth",
        feature_slug="001-add-auth",
        description="We need login",
        branch="001-add-auth",
        spec_dir="specs/001-add-auth",
        worktree_path="worktrees/001-add-auth",
        status="running",
        current_phase="specify",
    )
    session.add(workflow)
    await session.flush()
    phase = SpecPhase(workflow_id=workflow.id, phase="specify", status="pending")
    session.add(phase)
    await session.flush()
    return phase


# --- CommentService.add -----------------------------------------------------------------


async def test_add_persists_a_comment_on_a_run(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run = await make_run(session, workspace, owner)
    await session.commit()

    service = CommentService(session)
    comment = await service.add("run", run.id, owner.id, "Looks good")

    assert comment.body == "Looks good"
    assert comment.target_type == "run"
    assert comment.target_id == run.id
    assert comment.user_id == owner.id
    assert comment.resolved_at is None


async def test_add_resolves_pr_and_spec_phase_targets(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    pull_request = await make_pull_request(session, workspace)
    phase = await make_spec_phase(session, workspace, owner)
    await session.commit()

    service = CommentService(session)
    pr_comment = await service.add("pr", pull_request.id, owner.id, "Ship it")
    phase_comment = await service.add("spec_phase", phase.id, owner.id, "Needs a revision")

    assert pr_comment.target_id == pull_request.id
    assert phase_comment.target_id == phase.id


async def test_add_raises_for_an_invalid_target_type(db_session):
    service = CommentService(db_session)

    with pytest.raises(InvalidCommentTargetError):
        await service.add("workspace", uuid4(), uuid4(), "Hello")


async def test_add_raises_for_a_target_that_does_not_exist(db_session):
    service = CommentService(db_session)

    with pytest.raises(InvalidCommentTargetError):
        await service.add("run", uuid4(), uuid4(), "Hello")


async def test_add_denies_a_user_with_no_relationship_to_the_workspace(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run = await make_run(session, workspace, owner)
    stranger = await make_user(session, email=f"stranger-{uuid4()}@example.com")
    await session.commit()

    service = CommentService(session)
    with pytest.raises(WorkspaceNotFoundError):
        await service.add("run", run.id, stranger.id, "Hello")


async def test_add_notifies_the_run_owner_and_workspace_owner_but_not_the_author(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run_author = await make_user(session, email=f"runner-{uuid4()}@example.com")
    run = await make_run(session, workspace, run_author)
    await session.commit()

    service = CommentService(session)
    comment = await service.add("run", run.id, owner.id, "Nice work")
    await session.commit()

    result = await session.execute(
        select(Notification).where(Notification.workspace_id == workspace.id)
    )
    notifications = list(result.scalars().all())
    recipients = {n.user_id for n in notifications}

    assert recipients == {run_author.id}
    assert owner.id not in recipients
    assert all(n.notification_type == "comment_created" for n in notifications)
    assert comment.body == "Nice work"


# --- CommentService.list_for_target -------------------------------------------------------


async def test_list_for_target_returns_comments_oldest_first(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run = await make_run(session, workspace, owner)
    await session.commit()

    service = CommentService(session)
    first = await service.add("run", run.id, owner.id, "First")
    second = await service.add("run", run.id, owner.id, "Second")

    comments = await service.list_for_target("run", run.id, owner.id)

    assert [c.id for c in comments] == [first.id, second.id]


async def test_list_for_target_denies_a_stranger(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run = await make_run(session, workspace, owner)
    stranger = await make_user(session, email=f"stranger-{uuid4()}@example.com")
    await session.commit()

    service = CommentService(session)
    with pytest.raises(WorkspaceNotFoundError):
        await service.list_for_target("run", run.id, stranger.id)


# --- CommentService.resolve ---------------------------------------------------------------


async def test_resolve_sets_resolved_at_and_resolved_by(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run = await make_run(session, workspace, owner)
    await session.commit()

    service = CommentService(session)
    comment = await service.add("run", run.id, owner.id, "Please fix")

    resolved = await service.resolve(comment.id, owner.id)

    assert resolved.resolved_at is not None
    assert resolved.resolved_by == owner.id


async def test_resolve_is_idempotent_and_keeps_the_first_resolver(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run = await make_run(session, workspace, owner)
    await session.commit()

    service = CommentService(session)
    comment = await service.add("run", run.id, owner.id, "Please fix")
    first_resolution = await service.resolve(comment.id, owner.id)

    # Resolving again (e.g. a second click, or a retried request) must not clobber who
    # resolved it first or bump the resolved_at timestamp.
    second_resolution = await service.resolve(comment.id, owner.id)

    assert second_resolution.resolved_at == first_resolution.resolved_at
    assert second_resolution.resolved_by == owner.id


async def test_resolve_raises_for_an_unknown_comment(db_session):
    service = CommentService(db_session)

    with pytest.raises(CommentNotFoundError):
        await service.resolve(uuid4(), uuid4())


async def test_resolve_denies_a_stranger(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run = await make_run(session, workspace, owner)
    stranger = await make_user(session, email=f"stranger-{uuid4()}@example.com")
    await session.commit()

    service = CommentService(session)
    comment = await service.add("run", run.id, owner.id, "Please fix")

    with pytest.raises(WorkspaceNotFoundError):
        await service.resolve(comment.id, stranger.id)


# --- API-level tests -----------------------------------------------------------------------


async def test_create_comment_requires_auth():
    app.dependency_overrides.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/comments", json={"target_type": "run", "target_id": str(uuid4()), "body": "Hi"}
        )
    assert response.status_code == 401


async def test_create_list_and_resolve_comment_via_api(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run = await make_run(session, workspace, owner)
    await session.commit()

    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(owner)
    app.dependency_overrides[comments_module.get_comment_service] = comment_service_override(
        session
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/comments",
                json={"target_type": "run", "target_id": str(run.id), "body": "Looks great"},
            )
            listed = await client.get(
                "/comments", params={"target_type": "run", "target_id": str(run.id)}
            )
            resolved = await client.patch(f"/comments/{created.json()['id']}/resolve")
    finally:
        app.dependency_overrides.clear()

    assert created.status_code == 201
    assert created.json()["body"] == "Looks great"
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert resolved.status_code == 200
    assert resolved.json()["resolved_by"] == str(owner.id)


async def test_create_comment_rejects_an_empty_body_via_api(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run = await make_run(session, workspace, owner)
    await session.commit()

    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(owner)
    app.dependency_overrides[comments_module.get_comment_service] = comment_service_override(
        session
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/comments",
                json={"target_type": "run", "target_id": str(run.id), "body": "   "},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


async def test_create_comment_returns_404_for_a_stranger_via_api(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    workspace = await make_workspace(session, owner)
    run = await make_run(session, workspace, owner)
    stranger = await make_user(session, email=f"stranger-{uuid4()}@example.com")
    await session.commit()

    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
        stranger
    )
    app.dependency_overrides[comments_module.get_comment_service] = comment_service_override(
        session
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/comments",
                json={"target_type": "run", "target_id": str(run.id), "body": "Hi"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


async def test_create_comment_returns_404_for_an_unknown_target_via_api(db_session):
    session = db_session
    owner = await make_user(session, email=f"owner-{uuid4()}@example.com")
    await session.commit()

    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(owner)
    app.dependency_overrides[comments_module.get_comment_service] = comment_service_override(
        session
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/comments",
                json={"target_type": "run", "target_id": str(uuid4()), "body": "Hi"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
