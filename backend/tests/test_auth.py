from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request, status
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.core.security import hash_password, verify_password
from app.main import app
from app.models import User
from app.services.users import DuplicateUserEmailError


class FakeUserStore:
    def __init__(self) -> None:
        self.users_by_email: dict[str, User] = {}
        self.users_by_id: dict[str, User] = {}

    async def get_by_email(self, email: str) -> User | None:
        return self.users_by_email.get(email)

    async def get_by_id(self, user_id) -> User | None:
        return self.users_by_id.get(str(user_id))

    async def register_user(self, email: str, name: str | None, password_hash: str) -> User:
        if email in self.users_by_email:
            raise DuplicateUserEmailError

        now = datetime.now(UTC)
        user = User(
            id=uuid4(),
            email=email,
            name=name,
            password_hash=password_hash,
            role="owner" if not self.users_by_email else "viewer",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        self.users_by_email[email] = user
        self.users_by_id[str(user.id)] = user
        return user


class FakeRateLimiter:
    def __init__(self, blocked: bool = False) -> None:
        self.blocked = blocked
        self.checked = 0
        self.reset_count = 0

    async def check(self, email: str, request: Request) -> None:
        self.checked += 1
        if self.blocked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Please try again later.",
            )

    async def reset(self, email: str, request: Request) -> None:
        self.reset_count += 1


@pytest.fixture
def fake_user_store():
    store = FakeUserStore()
    app.dependency_overrides[auth_module.get_user_store] = lambda: store
    yield store
    app.dependency_overrides.clear()


@pytest.fixture
def fake_rate_limiter():
    limiter = FakeRateLimiter()
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: limiter
    yield limiter
    app.dependency_overrides.clear()


async def test_register_hashes_password_sets_cookie_and_me_returns_user(fake_user_store):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/auth/register",
            json={"email": "Rangga@Example.COM", "password": "correct horse", "name": " Rangga "},
        )

        assert response.status_code == 201
        payload = response.json()
        assert payload["email"] == "rangga@example.com"
        assert payload["name"] == "Rangga"
        assert payload["role"] == "owner"
        assert "execute_session" in response.headers["set-cookie"]

        stored_user = fake_user_store.users_by_email["rangga@example.com"]
        assert stored_user.password_hash != "correct horse"
        assert verify_password("correct horse", stored_user.password_hash)

        me_response = await client.get("/auth/me")

    assert me_response.status_code == 200
    assert me_response.json()["email"] == "rangga@example.com"


async def test_first_registered_user_is_owner_and_second_is_viewer(fake_user_store):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/auth/register",
            json={"email": "owner@example.com", "password": "password-one"},
        )
        second = await client.post(
            "/auth/register",
            json={"email": "viewer@example.com", "password": "password-two"},
        )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["role"] == "owner"
    assert second.json()["role"] == "viewer"


async def test_duplicate_registration_fails_safely(fake_user_store):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/auth/register",
            json={"email": "same@example.com", "password": "password-one"},
        )
        response = await client.post(
            "/auth/register",
            json={"email": "same@example.com", "password": "password-two"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unable to register with those credentials"


async def test_login_succeeds_and_invalid_credentials_fail_safely(
    fake_user_store,
    fake_rate_limiter,
):
    await fake_user_store.register_user(
        email="user@example.com",
        name=None,
        password_hash=hash_password("right-password"),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        invalid = await client.post(
            "/auth/login",
            json={"email": "user@example.com", "password": "wrong-password"},
        )
        valid = await client.post(
            "/auth/login",
            json={"email": "user@example.com", "password": "right-password"},
        )

    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "Invalid email or password"
    assert valid.status_code == 200
    assert valid.json()["email"] == "user@example.com"
    assert fake_rate_limiter.checked == 2
    assert fake_rate_limiter.reset_count == 1


async def test_login_rate_limit_returns_429(fake_user_store):
    limiter = FakeRateLimiter(blocked=True)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: limiter

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "user@example.com", "password": "password"},
        )

    app.dependency_overrides.clear()
    assert response.status_code == 429
    assert response.json()["detail"] == "Too many login attempts. Please try again later."


async def test_logout_clears_cookie_and_me_requires_auth(fake_user_store):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/auth/register",
            json={"email": "logout@example.com", "password": "password-one"},
        )
        logout_response = await client.post("/auth/logout")
        me_response = await client.get("/auth/me")

    assert logout_response.status_code == 200
    assert logout_response.json() == {"ok": True}
    assert "Max-Age=0" in logout_response.headers["set-cookie"]
    assert me_response.status_code == 401


async def test_role_guard_permits_allowed_roles_and_rejects_others():
    now = datetime.now(UTC)
    owner = User(
        id=uuid4(),
        email="owner@example.com",
        password_hash="hash",
        role="owner",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    viewer = User(
        id=uuid4(),
        email="viewer@example.com",
        password_hash="hash",
        role="viewer",
        is_active=True,
        created_at=now,
        updated_at=now,
    )

    guard = auth_module.require_role("owner")

    assert await guard(user=owner) is owner
    with pytest.raises(HTTPException) as error:
        await guard(user=viewer)

    assert error.value.status_code == 403
