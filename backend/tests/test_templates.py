import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import templates as templates_module
from app.core.security import hash_password
from app.main import app
from app.models import User, WorkspaceTemplate
from app.services.git_providers import parse_repository_url
from app.services.users import DuplicateUserEmailError


class FakeUserStore:
    def __init__(self, user: User | None = None) -> None:
        self.user = user

    async def get_by_email(self, email: str) -> User | None:
        return self.user if self.user and self.user.email == email else None

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self.user if self.user and self.user.id == user_id else None

    async def register_user(self, email: str, name: str | None, password_hash: str) -> User:
        raise DuplicateUserEmailError


class FakeRateLimiter:
    async def check(self, email: str, request) -> None:
        return None

    async def reset(self, email: str, request) -> None:
        return None


class FakeTemplateService:
    def __init__(self, templates: list[WorkspaceTemplate]) -> None:
        self.templates = templates

    async def list_active(self) -> list[WorkspaceTemplate]:
        active = [t for t in self.templates if t.is_active]
        return sorted(active, key=lambda t: (t.sort_order, t.name))


def _template(slug: str, sort_order: int, *, active: bool = True) -> WorkspaceTemplate:
    now = datetime.now(UTC)
    return WorkspaceTemplate(
        id=uuid4(),
        slug=slug,
        name=slug.title(),
        description=f"{slug} starter",
        source_repo_url=f"https://github.com/example/{slug}.git",
        default_branch="main",
        tags=["demo"],
        sort_order=sort_order,
        is_active=active,
        created_at=now,
        updated_at=now,
    )


def _user() -> User:
    return User(
        id=uuid4(),
        email="owner@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


async def test_list_templates_requires_auth():
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/workspace-templates")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 401


async def test_list_templates_returns_active_sorted():
    user = _user()
    service = FakeTemplateService(
        [_template("zeta", 20), _template("alpha", 10), _template("hidden", 0, active=False)]
    )
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[templates_module.get_template_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/auth/login", json={"email": "owner@example.com", "password": "password"}
            )
            response = await client.get("/workspace-templates")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert [item["slug"] for item in body] == ["alpha", "zeta"]
    assert body[0]["source_repo_url"] == "https://github.com/example/alpha.git"
    assert body[0]["tags"] == ["demo"]
    assert "mise_config" not in body[0]


def test_seed_catalog_is_consistent():
    """The migration's built-in catalog must have unique slugs, valid URLs, and a ``dev`` task."""
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0022_templates_and_git_providers.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0022", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    seeds = module.SEED_TEMPLATES
    assert len(seeds) >= 3
    assert len({row["slug"] for row in seeds}) == len(seeds)
    assert len({row["id"] for row in seeds}) == len(seeds)
    for row in seeds:
        parsed = parse_repository_url(row["source_repo_url"])
        assert parsed.provider == "github"
        assert "[tasks.dev]" in row["mise_config"]
        assert row["preview_port"] is not None
