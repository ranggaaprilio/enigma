"""Epic 06: expensive endpoints carry a per-(user, workspace) rate limit."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api.rate_limits import enforce_sandbox_rate_limit, enforce_spec_run_rate_limit
from app.main import app

SPEC_RUN_ROUTES = [
    ("POST", "/workspaces/{workspace_id}/spec/workflows"),
    ("POST", "/workspaces/{workspace_id}/spec/workflows/{workflow_id}/phases/{phase}/run"),
    ("POST", "/workspaces/{workspace_id}/spec/workflows/{workflow_id}/phases/analyze/answers"),
    (
        "POST",
        "/workspaces/{workspace_id}/spec/workflows/{workflow_id}/phases/{phase}/request-changes",
    ),
]
SANDBOX_ROUTES = [
    ("POST", "/workspaces/{workspace_id}/files/write"),
    ("POST", "/workspaces/{workspace_id}/files/upload"),
    ("POST", "/workspaces/{workspace_id}/editor/start"),
    ("POST", "/workspaces/{workspace_id}/dev/setup"),
    ("POST", "/workspaces/{workspace_id}/dev/run"),
    ("POST", "/workspaces/{workspace_id}/dev/ensure"),
    ("POST", "/workspaces/{workspace_id}/services/ensure"),
    ("POST", "/workspaces/{workspace_id}/services/{service_id}/start"),
    ("POST", "/workspaces/{workspace_id}/services/{service_id}/restart"),
    ("POST", "/workspaces/{workspace_id}/terminal/sessions"),
]


def _route_dependencies(method: str, path: str) -> set:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return {dep.call for dep in route.dependant.dependencies}
    raise AssertionError(f"route not found: {method} {path}")


@pytest.mark.parametrize(("method", "path"), SPEC_RUN_ROUTES)
def test_spec_run_triggers_are_rate_limited(method, path):
    assert enforce_spec_run_rate_limit in _route_dependencies(method, path)


@pytest.mark.parametrize(("method", "path"), SANDBOX_ROUTES)
def test_sandbox_actions_are_rate_limited(method, path):
    assert enforce_sandbox_rate_limit in _route_dependencies(method, path)


async def test_rate_limited_route_returns_429_with_retry_after():
    def limited() -> None:
        raise HTTPException(status_code=429, detail="slow down", headers={"Retry-After": "9"})

    app.dependency_overrides[auth_module.get_current_user] = lambda: None
    app.dependency_overrides[enforce_sandbox_rate_limit] = limited
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/workspaces/{uuid4()}/editor/start")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "9"
