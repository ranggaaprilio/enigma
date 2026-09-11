"""Global LLM settings: storage, resolution precedence, the owner-only API, and the probe.

The service half uses the real transactional ``db_session`` because the singleton constraints
and the Fernet round-trip are the point -- an in-memory fake would assert nothing. The probe
half uses a local ``ThreadingHTTPServer`` (the pattern from ``test_openhands_provider.py``) so
the HTTP-status-to-message mapping is exercised for real without any network access.
"""

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth as auth_module
from app.api import platform_settings as platform_settings_module
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models import PlatformLlmSettings, User
from app.services.agents import AgentConfigurationError, provider_for_run
from app.services.platform_llm import (
    InvalidLlmBaseUrlError,
    LlmSettingsDecryptionError,
    PlatformLlmSettingsService,
    validate_base_url,
)
from app.services.workspaces import WorkspaceSecretCipher

REAL_KEY = "sk-super-secret-value-12345"
OTHER_ENCRYPTION_KEY = "n2m3ZBk2mX0oF8cQKk0nJ3wZzT5Xc9uQ1pW7bQ9Yt4A="


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


async def make_user(session, role: str = "owner") -> User:
    user = User(
        id=uuid4(),
        email=f"{role}-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password"),
        role=role,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    return user


def cipher(key: str | None = None) -> WorkspaceSecretCipher:
    return WorkspaceSecretCipher(key or get_settings().workspace_env_encryption_key)


def bare_settings(**kwargs) -> Settings:
    """Settings with the LLM environment explicitly empty.

    Importing ``openhands.sdk`` calls ``load_dotenv()``, which injects the repo-root ``.env``
    into ``os.environ`` -- so a bare ``Settings()`` silently picks up ``OPENHANDS_LLM_*`` once
    any test has touched the OpenHands provider. Pinning the fields here keeps "nothing is
    configured" meaning exactly that, whatever the import order.
    """
    kwargs.setdefault("agent_provider", "auto")
    kwargs.setdefault("openhands_llm_base_url", None)
    kwargs.setdefault("openhands_llm_model", None)
    kwargs.setdefault("openhands_llm_api_key", None)
    kwargs.setdefault("openai_api_key", None)
    return Settings(**kwargs)


def make_service(session, settings: Settings | None = None) -> PlatformLlmSettingsService:
    return PlatformLlmSettingsService(
        session=session,
        cipher=cipher(),
        settings=settings if settings is not None else bare_settings(),
    )


def _override_current_user(user: User):
    async def _override():
        return user

    return _override


def _override_db(session):
    async def _override():
        yield session

    return _override


def _override_service(session, settings: Settings | None = None):
    async def _override():
        return make_service(session, settings)

    return _override


def env_settings(**kwargs) -> Settings:
    return bare_settings(
        agent_provider="openhands",
        openhands_llm_base_url="http://env.example/v1",
        openhands_llm_model="env-model",
        openhands_llm_api_key="env-key",
        **kwargs,
    )


# --- validate_base_url --------------------------------------------------------------------


def test_validate_base_url_strips_trailing_slash():
    assert validate_base_url("https://api.openai.com/v1/") == "https://api.openai.com/v1"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "file:///etc/passwd",
        "ftp://example.com/v1",
        "not-a-url",
        # Cloud metadata endpoints are the one host class we refuse outright.
        "http://169.254.169.254/v1",
        "http://metadata.google.internal/v1",
    ],
)
def test_validate_base_url_rejects(bad):
    with pytest.raises(InvalidLlmBaseUrlError):
        validate_base_url(bad)


def test_validate_base_url_allows_loopback():
    """Local runtimes are the primary use case, so loopback must never be blocked."""
    assert validate_base_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert validate_base_url("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/v1"


# --- resolution precedence ----------------------------------------------------------------


async def test_resolve_returns_none_when_nothing_configured(db_session):
    assert await make_service(db_session).resolve() is None


async def test_resolve_uses_environment_when_no_row(db_session):
    resolved = await make_service(db_session, env_settings()).resolve()
    assert resolved is not None
    assert resolved.source == "environment"
    assert resolved.base_url == "http://env.example/v1"
    assert resolved.model == "env-model"
    assert resolved.api_key == "env-key"


async def test_database_row_wins_over_environment(db_session):
    user = await make_user(db_session)
    service = make_service(db_session, env_settings())
    await service.upsert(
        base_url="http://db.example/v1", model="db-model", api_key=REAL_KEY, user_id=user.id
    )

    resolved = await service.resolve()
    assert resolved is not None
    assert resolved.source == "database"
    assert resolved.base_url == "http://db.example/v1"
    assert resolved.model == "db-model"
    assert resolved.api_key == REAL_KEY


# --- storage ------------------------------------------------------------------------------


async def test_upsert_encrypts_the_api_key(db_session):
    user = await make_user(db_session)
    service = make_service(db_session)
    row = await service.upsert(
        base_url="http://x.example/v1", model="m", api_key=REAL_KEY, user_id=user.id
    )

    assert row.encrypted_api_key is not None
    assert REAL_KEY not in row.encrypted_api_key
    assert cipher().decrypt(row.encrypted_api_key) == REAL_KEY
    assert row.updated_by_user_id == user.id


async def test_upsert_is_a_singleton(db_session):
    user = await make_user(db_session)
    service = make_service(db_session)
    await service.upsert(base_url="http://a.example/v1", model="a", api_key=None, user_id=user.id)
    await service.upsert(base_url="http://b.example/v1", model="b", api_key=None, user_id=user.id)

    count = await db_session.scalar(select(func.count()).select_from(PlatformLlmSettings))
    assert count == 1
    row = await service.get_row()
    assert row is not None
    assert row.base_url == "http://b.example/v1"


async def test_api_key_tristate(db_session):
    """None keeps the stored key, "" clears it, a value replaces it."""
    user = await make_user(db_session)
    service = make_service(db_session)
    await service.upsert(
        base_url="http://x.example/v1", model="m", api_key=REAL_KEY, user_id=user.id
    )

    # None -> preserved.
    row = await service.upsert(
        base_url="http://x.example/v1", model="m2", api_key=None, user_id=user.id
    )
    assert cipher().decrypt(row.encrypted_api_key) == REAL_KEY

    # "" -> cleared.
    row = await service.upsert(
        base_url="http://x.example/v1", model="m2", api_key="", user_id=user.id
    )
    assert row.encrypted_api_key is None

    # value -> replaced.
    row = await service.upsert(
        base_url="http://x.example/v1", model="m2", api_key="sk-new", user_id=user.id
    )
    assert cipher().decrypt(row.encrypted_api_key) == "sk-new"


async def test_upsert_rejects_bad_input(db_session):
    user = await make_user(db_session)
    service = make_service(db_session)
    with pytest.raises(InvalidLlmBaseUrlError):
        await service.upsert(base_url="nope", model="m", api_key=None, user_id=user.id)


async def test_clear_falls_back_to_environment(db_session):
    user = await make_user(db_session)
    service = make_service(db_session, env_settings())
    await service.upsert(
        base_url="http://db.example/v1", model="db-model", api_key=None, user_id=user.id
    )

    assert await service.clear() is True
    resolved = await service.resolve()
    assert resolved is not None
    assert resolved.source == "environment"
    assert await service.clear() is False


async def test_undecryptable_key_raises(db_session):
    """A rotated encryption key must produce an actionable error, never a silent None key."""
    user = await make_user(db_session)
    written_with_other_key = PlatformLlmSettingsService(
        session=db_session, cipher=cipher(OTHER_ENCRYPTION_KEY), settings=bare_settings()
    )
    await written_with_other_key.upsert(
        base_url="http://x.example/v1", model="m", api_key=REAL_KEY, user_id=user.id
    )

    with pytest.raises(LlmSettingsDecryptionError):
        await make_service(db_session).resolve()


# --- the agent seam -----------------------------------------------------------------------


async def test_provider_for_run_uses_the_stored_config(db_session):
    user = await make_user(db_session)
    await make_service(db_session).upsert(
        base_url="http://db.example/v1", model="db-model", api_key=REAL_KEY, user_id=user.id
    )

    provider = await provider_for_run(db_session, bare_settings())
    assert provider.provider_name == "openhands"
    assert provider.base_url == "http://db.example/v1"
    assert provider.api_key == REAL_KEY
    # normalize_model_name prefixes a bare id so LiteLLM routes it as OpenAI-compatible.
    assert provider.model_name == "openai/db-model"


async def test_provider_for_run_refuses_when_unconfigured(db_session):
    with pytest.raises(AgentConfigurationError, match="No LLM is configured"):
        await provider_for_run(db_session, bare_settings())


async def test_provider_for_run_allows_mock_when_opted_in(db_session):
    provider = await provider_for_run(db_session, bare_settings(agent_allow_mock_provider=True))
    assert provider.provider_name == "mock"


async def test_provider_for_run_still_honours_the_environment(db_session):
    provider = await provider_for_run(db_session, env_settings())
    assert provider.provider_name == "openhands"
    assert provider.base_url == "http://env.example/v1"


# --- API: authorization -------------------------------------------------------------------


async def client_for(session, user, settings: Settings | None = None):
    app.dependency_overrides[auth_module.get_current_user] = _override_current_user(user)
    app.dependency_overrides[get_db_session] = _override_db(session)
    app.dependency_overrides[platform_settings_module.get_platform_llm_service] = _override_service(
        session, settings
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("role", ["developer", "viewer"])
async def test_non_owner_cannot_read_or_write_settings(db_session, role):
    user = await make_user(db_session, role=role)
    try:
        async with await client_for(db_session, user) as http:
            assert (await http.get("/platform/llm-settings")).status_code == 403
            put = await http.put(
                "/platform/llm-settings",
                json={"base_url": "http://x.example/v1", "model": "m"},
            )
            assert put.status_code == 403
            assert (await http.delete("/platform/llm-settings")).status_code == 403
            assert (await http.post("/platform/llm-settings/test", json={})).status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("role", ["owner", "developer", "viewer"])
async def test_any_authenticated_user_can_read_status(db_session, role):
    user = await make_user(db_session, role=role)
    try:
        async with await client_for(db_session, user) as http:
            response = await http.get("/platform/llm-settings/status")
            assert response.status_code == 200
            assert response.json() == {
                "configured": False,
                "source": None,
                "base_url": None,
                "model": None,
            }
    finally:
        app.dependency_overrides.clear()


async def test_status_never_exposes_the_api_key(db_session):
    owner = await make_user(db_session)
    await make_service(db_session).upsert(
        base_url="http://db.example/v1", model="db-model", api_key=REAL_KEY, user_id=owner.id
    )
    viewer = await make_user(db_session, role="viewer")
    try:
        async with await client_for(db_session, viewer) as http:
            response = await http.get("/platform/llm-settings/status")
            assert response.status_code == 200
            assert REAL_KEY not in response.text
            body = response.json()
            assert body["configured"] is True
            assert body["source"] == "database"
            assert body["base_url"] == "http://db.example/v1"
    finally:
        app.dependency_overrides.clear()


# --- API: owner flows ---------------------------------------------------------------------


async def test_get_returns_nulls_when_unconfigured(db_session):
    owner = await make_user(db_session)
    try:
        async with await client_for(db_session, owner) as http:
            body = (await http.get("/platform/llm-settings")).json()
            assert body["base_url"] is None
            assert body["has_api_key"] is False
            assert body["source"] is None
    finally:
        app.dependency_overrides.clear()


async def test_put_then_get_never_returns_the_key(db_session):
    owner = await make_user(db_session)
    try:
        async with await client_for(db_session, owner) as http:
            put = await http.put(
                "/platform/llm-settings",
                json={"base_url": "http://db.example/v1", "model": "m", "api_key": REAL_KEY},
            )
            assert put.status_code == 200
            assert REAL_KEY not in put.text
            assert put.json()["masked_api_key"] == "********"

            get = await http.get("/platform/llm-settings")
            assert REAL_KEY not in get.text
            body = get.json()
            assert body["has_api_key"] is True
            assert body["masked_api_key"] == "********"
            assert body["source"] == "database"
    finally:
        app.dependency_overrides.clear()


async def test_put_without_api_key_keeps_the_stored_one(db_session):
    owner = await make_user(db_session)
    try:
        async with await client_for(db_session, owner) as http:
            await http.put(
                "/platform/llm-settings",
                json={"base_url": "http://db.example/v1", "model": "m", "api_key": REAL_KEY},
            )
            # A client that cannot read the key back sends no key at all.
            response = await http.put(
                "/platform/llm-settings",
                json={"base_url": "http://db.example/v1", "model": "m2"},
            )
            assert response.json()["has_api_key"] is True

        row = await make_service(db_session).get_row()
        assert cipher().decrypt(row.encrypted_api_key) == REAL_KEY
    finally:
        app.dependency_overrides.clear()


async def test_put_with_empty_api_key_clears_it(db_session):
    owner = await make_user(db_session)
    try:
        async with await client_for(db_session, owner) as http:
            await http.put(
                "/platform/llm-settings",
                json={"base_url": "http://db.example/v1", "model": "m", "api_key": REAL_KEY},
            )
            response = await http.put(
                "/platform/llm-settings",
                json={"base_url": "http://db.example/v1", "model": "m", "api_key": ""},
            )
            assert response.json()["has_api_key"] is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"base_url": "file:///etc/passwd", "model": "m"}, 400),
        ({"base_url": "http://169.254.169.254/v1", "model": "m"}, 400),
        ({"base_url": "http://x.example/v1"}, 422),
        ({"base_url": "", "model": "m"}, 422),
    ],
)
async def test_put_rejects_bad_payloads(db_session, payload, expected):
    owner = await make_user(db_session)
    try:
        async with await client_for(db_session, owner) as http:
            response = await http.put("/platform/llm-settings", json=payload)
            assert response.status_code == expected
    finally:
        app.dependency_overrides.clear()


async def test_delete_clears_configuration(db_session):
    owner = await make_user(db_session)
    try:
        async with await client_for(db_session, owner) as http:
            await http.put(
                "/platform/llm-settings",
                json={"base_url": "http://db.example/v1", "model": "m"},
            )
            assert (await http.delete("/platform/llm-settings")).json() == {
                "ok": True,
                "cleared": True,
            }
            assert (await http.delete("/platform/llm-settings")).json()["cleared"] is False
    finally:
        app.dependency_overrides.clear()


async def test_undecryptable_key_surfaces_as_503(db_session):
    owner = await make_user(db_session)
    await PlatformLlmSettingsService(
        session=db_session, cipher=cipher(OTHER_ENCRYPTION_KEY), settings=bare_settings()
    ).upsert(base_url="http://x.example/v1", model="m", api_key=REAL_KEY, user_id=owner.id)
    try:
        async with await client_for(db_session, owner) as http:
            assert (await http.get("/platform/llm-settings")).status_code == 503
            # The gate must not trip just because the key is unreadable: the endpoint IS set.
            status = await http.get("/platform/llm-settings/status")
            assert status.status_code == 200
            assert status.json()["configured"] is True
    finally:
        app.dependency_overrides.clear()


async def test_audit_metadata_survives_safe_metadata(db_session):
    """Field names containing key/secret/credential are silently dropped by safe_metadata."""
    from app.models import AuditLog

    owner = await make_user(db_session)
    try:
        async with await client_for(db_session, owner) as http:
            await http.put(
                "/platform/llm-settings",
                json={"base_url": "http://db.example/v1", "model": "m", "api_key": REAL_KEY},
            )
        entry = (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.action == "platform.llm_settings.updated")
                .order_by(AuditLog.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        assert entry is not None
        assert entry.log_metadata["base_url"] == "http://db.example/v1"
        assert entry.log_metadata["auth_replaced"] is True
        assert REAL_KEY not in json.dumps(entry.log_metadata)
    finally:
        app.dependency_overrides.clear()


# --- the connection probe -----------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    models_status = 200
    completion_status = 200
    seen_auth: list[str | None] = []

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        type(self).seen_auth.append(self.headers.get("Authorization"))
        if self.path.endswith("/models"):
            if self.models_status != 200:
                self._send(self.models_status, {"error": "nope"})
                return
            self._send(200, {"data": [{"id": "beta"}, {"id": "alpha"}]})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._send(self.completion_status, {"choices": [{"message": {"content": "pong"}}]})


class _Server:
    def __init__(self, **attrs):
        for key, value in attrs.items():
            setattr(_Handler, key, value)
        _Handler.seen_auth = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


async def test_probe_lists_models(db_session):
    with _Server(models_status=200) as server:
        result = await make_service(db_session).probe(server.url, None)
    assert result.ok is True
    assert result.models == ["alpha", "beta"]
    assert result.latency_ms is not None


async def test_probe_sends_bearer_only_when_a_key_is_present(db_session):
    with _Server(models_status=200) as server:
        await make_service(db_session).probe(server.url, None)
        assert _Handler.seen_auth[-1] is None
        await make_service(db_session).probe(server.url, "k")
        assert _Handler.seen_auth[-1] == "Bearer k"


async def test_probe_reports_a_rejected_key(db_session):
    with _Server(models_status=401) as server:
        result = await make_service(db_session).probe(server.url, "bad")
    assert result.ok is False
    assert "rejected the API key" in result.message
    assert "bad" not in result.message


async def test_probe_falls_back_to_a_completion_when_models_is_missing(db_session):
    with _Server(models_status=404, completion_status=200) as server:
        result = await make_service(db_session).probe(server.url, None, "some-model")
    assert result.ok is True
    assert 'Model "some-model" answered' in result.message


async def test_probe_reports_a_rejected_model(db_session):
    with _Server(models_status=404, completion_status=400) as server:
        result = await make_service(db_session).probe(server.url, None, "ghost")
    assert result.ok is False
    assert "ghost" in result.message


async def test_probe_without_models_or_model_suggests_the_v1_suffix(db_session):
    with _Server(models_status=404) as server:
        result = await make_service(db_session).probe(server.url, None)
    assert result.ok is False
    assert "/v1" in result.message


async def test_probe_reports_a_refused_connection(db_session):
    # Bind then immediately release a port so nothing is listening on it.
    closed = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = closed.server_port
    closed.server_close()

    result = await make_service(db_session).probe(f"http://127.0.0.1:{port}/v1", None)
    assert result.ok is False
    assert "Could not reach the endpoint" in result.message
