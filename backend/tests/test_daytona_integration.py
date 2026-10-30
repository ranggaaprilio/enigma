import os
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.models import Workspace
from app.services.sandbox.daytona import DaytonaRuntime

pytestmark = pytest.mark.skipif(
    os.getenv("DAYTONA_INTEGRATION_TESTS") != "1",
    reason="Set DAYTONA_INTEGRATION_TESTS=1 to run against Daytona",
)


async def test_real_daytona_sandbox_lifecycle_and_command() -> None:
    settings = get_settings()
    if not settings.daytona_api_key:
        pytest.fail("DAYTONA_API_KEY is required for integration tests")
    runtime = DaytonaRuntime(settings)
    workspace_id = uuid4()
    owner_id = uuid4()
    workspace = Workspace(
        id=workspace_id,
        owner_id=owner_id,
        name="Integration",
        slug="integration",
        repo_url="https://github.com/octocat/Hello-World.git",
        default_branch="master",
        runtime="daytona",
        snapshot=settings.daytona_default_snapshot,
        workspace_path=settings.sandbox_workspace_path,
        status="creating",
    )
    try:
        workspace.sandbox_id = await runtime.provision(workspace)
        result = await runtime.exec(
            workspace,
            "printf daytona-ready",
            cwd=workspace.workspace_path,
            timeout=10,
        )
        assert result.exit_code == 0
        assert result.stdout == "daytona-ready"
    finally:
        if workspace.daytona_sandbox_id:
            await runtime.delete(workspace)
        await runtime.close()
