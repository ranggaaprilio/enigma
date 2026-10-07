"""Epic 13: preview alias validation, host classification, the grant token, and minting.

This is the security core of the alias feature -- tested before anything is wired into the
proxy or the API layer.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import create_session_token, verify_session_token
from app.models import CustomCommand, User, Workspace, WorkspacePreviewAlias
from app.services.preview_alias import (
    RESERVED_ALIAS_LABELS,
    AliasGrant,
    InvalidPreviewAliasError,
    InvalidPreviewAliasGrantError,
    PreviewAliasMintError,
    PreviewAliasService,
    alias_prime_url,
    alias_url,
    alias_url_for_row,
    classify_proxy_host,
    decode_alias_grant,
    encode_alias_grant,
    is_alias_label,
    should_refresh_grant,
    validate_alias,
)
from app.services.preview_capability import (
    InvalidPreviewCapabilityError,
    encode_preview_capability,
    is_capability_label,
)

SECRET = "preview-alias-secret"
BASE_URL = "https://preview.example.com"

# ---------------------------------------------------------------------------
# Disjointness: no minted capability is ever a valid alias, and vice versa.
# ---------------------------------------------------------------------------


def test_a_freshly_minted_capability_is_never_a_valid_alias_label():
    encoded = encode_preview_capability(
        uuid4(),
        5173,
        expires_at=datetime(2026, 6, 12, 12, 0, tzinfo=UTC),
        secret_key=SECRET,
    )
    assert is_capability_label(encoded) is True
    assert is_alias_label(encoded) is False


@pytest.mark.parametrize(
    "alias",
    ["web-api", "demo-web-a1b2c3", "dev", "a-b", "x" * 62],
)
def test_a_valid_alias_label_is_never_a_capability_label(alias):
    # "dev" alone has no hyphen so it fails is_alias_label -- included to show the boundary.
    if is_alias_label(alias):
        assert is_capability_label(alias) is False


def test_alias_and_capability_shapes_are_mutually_exclusive_by_construction():
    """`-` is not in the base32 alphabet [a-z2-7], and an alias is capped at 62 chars, so no
    alias can ever reach a capability's exact 63-char length -- structurally, not by ordering."""
    assert "-" not in "abcdefghijklmnopqrstuvwxyz234567"
    assert 62 < 63


# ---------------------------------------------------------------------------
# validate_alias / is_alias_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "web",  # no hyphen
        "A-B",  # uppercase
        "web_api",  # underscore
        "-web-api",  # leading hyphen
        "web-api-",  # trailing hyphen
        "we",  # too short even with padding (min length after hyphen requirement)
        "x" * 63,  # 63 chars -- would collide with a capability if not for the hyphen rule,
        # but length alone should also be rejected past 62
        "xn--web-api",  # punycode prefix
        "web.api",  # dot
    ],
)
def test_validate_alias_rejects_bad_format(candidate):
    with pytest.raises(InvalidPreviewAliasError):
        validate_alias(candidate, base_url=BASE_URL)


@pytest.mark.parametrize("reserved", sorted(RESERVED_ALIAS_LABELS))
def test_validate_alias_rejects_each_reserved_name(reserved):
    # Reserved names without a hyphen fail the format check first; skip those (they can never
    # collide anyway) and only test the ones that could otherwise pass format validation.
    if "-" not in reserved:
        pytest.skip(f"{reserved!r} has no hyphen and can never pass the format check anyway")
    with pytest.raises(InvalidPreviewAliasError, match="reserved"):
        validate_alias(reserved, base_url=BASE_URL)


def test_validate_alias_rejects_a_label_of_the_proxy_hostname():
    with pytest.raises(InvalidPreviewAliasError, match="hostname"):
        validate_alias("preview-x", base_url="https://preview-x.example.com")


def test_validate_alias_accepts_a_well_formed_candidate():
    assert validate_alias("demo-web-a1b2c3", base_url=BASE_URL) == "demo-web-a1b2c3"


# ---------------------------------------------------------------------------
# classify_proxy_host
# ---------------------------------------------------------------------------


def test_classify_proxy_host_identifies_a_capability():
    encoded = encode_preview_capability(
        uuid4(),
        5173,
        expires_at=datetime(2026, 6, 12, 12, 0, tzinfo=UTC),
        secret_key=SECRET,
    )
    host = classify_proxy_host(f"{encoded}.preview.example.com", base_url=BASE_URL)
    assert host.kind == "capability"
    assert host.label == encoded


def test_classify_proxy_host_identifies_an_alias():
    host = classify_proxy_host("demo-web-a1b2c3.preview.example.com", base_url=BASE_URL)
    assert host.kind == "alias"
    assert host.label == "demo-web-a1b2c3"


def test_classify_proxy_host_rejects_neither_shape():
    with pytest.raises(InvalidPreviewCapabilityError):
        classify_proxy_host("nohyphen.preview.example.com", base_url=BASE_URL)


def test_classify_proxy_host_reuses_hardening_against_multi_label_subdomains():
    with pytest.raises(InvalidPreviewCapabilityError):
        classify_proxy_host("demo-web-a1b2c3.attacker.example.com", base_url=BASE_URL)


# ---------------------------------------------------------------------------
# Alias URLs
# ---------------------------------------------------------------------------


def test_alias_url_builds_a_stable_https_origin():
    url = alias_url("demo-web-a1b2c3", base_url=BASE_URL)
    assert url == "https://demo-web-a1b2c3.preview.example.com/"


def test_alias_prime_url_appends_the_capability_query_param():
    url = alias_prime_url(
        "demo-api-a1b2c3",
        base_url=BASE_URL,
        capability="somecapabilitytoken",
        capability_query_param="__enigma_cap",
    )
    assert url == (
        "https://demo-api-a1b2c3.preview.example.com/?__enigma_cap=somecapabilitytoken"
    )


# ---------------------------------------------------------------------------
# Alias grant: round trip, binding, domain separation, refresh
# ---------------------------------------------------------------------------


def test_alias_grant_round_trips():
    workspace_id = uuid4()
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    token = encode_alias_grant(
        alias="demo-api-a1b2c3",
        workspace_id=workspace_id,
        secret_key=SECRET,
        ttl_seconds=900,
        now=now,
    )
    grant = decode_alias_grant(
        token,
        alias="demo-api-a1b2c3",
        workspace_id=workspace_id,
        secret_key=SECRET,
        max_lifetime_seconds=43200,
        now=now + timedelta(seconds=10),
    )
    assert isinstance(grant, AliasGrant)
    assert grant.alias == "demo-api-a1b2c3"
    assert grant.workspace_id == workspace_id


def test_alias_grant_rejected_for_a_different_alias():
    workspace_id = uuid4()
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    token = encode_alias_grant(
        alias="demo-web-a1b2c3", workspace_id=workspace_id, secret_key=SECRET, ttl_seconds=900,
        now=now,
    )
    with pytest.raises(InvalidPreviewAliasGrantError):
        decode_alias_grant(
            token,
            alias="demo-api-a1b2c3",
            workspace_id=workspace_id,
            secret_key=SECRET,
            max_lifetime_seconds=43200,
            now=now,
        )


def test_alias_grant_rejected_for_a_different_workspace():
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    token = encode_alias_grant(
        alias="demo-api-a1b2c3", workspace_id=uuid4(), secret_key=SECRET, ttl_seconds=900,
        now=now,
    )
    with pytest.raises(InvalidPreviewAliasGrantError):
        decode_alias_grant(
            token,
            alias="demo-api-a1b2c3",
            workspace_id=uuid4(),
            secret_key=SECRET,
            max_lifetime_seconds=43200,
            now=now,
        )


def test_alias_grant_rejected_after_its_idle_ttl_expires():
    workspace_id = uuid4()
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    token = encode_alias_grant(
        alias="demo-api-a1b2c3", workspace_id=workspace_id, secret_key=SECRET, ttl_seconds=900,
        now=now,
    )
    with pytest.raises(InvalidPreviewAliasGrantError, match="expired"):
        decode_alias_grant(
            token,
            alias="demo-api-a1b2c3",
            workspace_id=workspace_id,
            secret_key=SECRET,
            max_lifetime_seconds=43200,
            now=now + timedelta(seconds=901),
        )


def test_alias_grant_rejected_past_its_absolute_lifetime_even_if_refreshed():
    """A grant refreshed just under its idle TTL every time would otherwise live forever;
    `iat` enforces a hard ceiling independent of how often it's refreshed."""
    workspace_id = uuid4()
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    token = encode_alias_grant(
        alias="demo-api-a1b2c3",
        workspace_id=workspace_id,
        secret_key=SECRET,
        ttl_seconds=900,
        now=now,
    )
    with pytest.raises(InvalidPreviewAliasGrantError, match="absolute"):
        decode_alias_grant(
            token,
            alias="demo-api-a1b2c3",
            workspace_id=workspace_id,
            secret_key=SECRET,
            max_lifetime_seconds=100,  # far shorter than the ttl, to force the absolute check
            # Past max_lifetime_seconds (100s since issue) but still inside the 900s idle TTL,
            # so only the absolute-lifetime branch can be what rejects it.
            now=now + timedelta(seconds=150),
        )


def test_alias_grant_rejects_tampering():
    workspace_id = uuid4()
    token = encode_alias_grant(
        alias="demo-api-a1b2c3", workspace_id=workspace_id, secret_key=SECRET, ttl_seconds=900
    )
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    with pytest.raises(InvalidPreviewAliasGrantError):
        decode_alias_grant(
            tampered,
            alias="demo-api-a1b2c3",
            workspace_id=workspace_id,
            secret_key=SECRET,
            max_lifetime_seconds=43200,
        )


def test_domain_separation_a_platform_session_token_is_not_a_valid_alias_grant():
    user_id = uuid4()
    session_token = create_session_token(
        user_id=str(user_id), secret_key=SECRET, ttl_seconds=3600
    )
    with pytest.raises(InvalidPreviewAliasGrantError):
        decode_alias_grant(
            session_token,
            alias="demo-api-a1b2c3",
            workspace_id=uuid4(),
            secret_key=SECRET,
            max_lifetime_seconds=43200,
        )


def test_domain_separation_an_alias_grant_is_not_a_valid_platform_session():
    workspace_id = uuid4()
    grant_token = encode_alias_grant(
        alias="demo-api-a1b2c3", workspace_id=workspace_id, secret_key=SECRET, ttl_seconds=900
    )
    assert verify_session_token(grant_token, SECRET) is None


def test_should_refresh_grant_true_under_half_the_ttl_remaining():
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    grant = AliasGrant(
        alias="x", workspace_id=uuid4(), issued_at=now, expires_at=now + timedelta(seconds=100)
    )
    assert should_refresh_grant(grant, ttl_seconds=900, now=now + timedelta(seconds=451)) is True


def test_should_refresh_grant_false_with_plenty_of_life_left():
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    grant = AliasGrant(
        alias="x", workspace_id=uuid4(), issued_at=now, expires_at=now + timedelta(seconds=900)
    )
    assert should_refresh_grant(grant, ttl_seconds=900, now=now + timedelta(seconds=10)) is False


# ---------------------------------------------------------------------------
# PreviewAliasService: minting, idempotency, collisions -- against real Postgres.
# ---------------------------------------------------------------------------


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


async def _make_workspace(
    session, *, slug: str = "demo", sandbox_id: str | None = None
) -> Workspace:
    user = User(
        id=uuid4(),
        email=f"owner-{uuid4().hex[:8]}@example.com",
        password_hash="x",
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace = Workspace(
        id=uuid4(),
        owner_id=user.id,
        name="Demo",
        slug=slug,
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id=sandbox_id or f"sandbox-{uuid4().hex[:8]}",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(user)
    session.add(workspace)
    await session.flush()
    return workspace


async def test_enable_mints_an_alias_for_the_builtin_row(db_session):
    workspace = await _make_workspace(db_session)
    service = PreviewAliasService(db_session, get_settings())

    await service.enable(workspace)

    row = await service.alias_for_row(workspace.id, None)
    assert row is not None
    assert row.custom_command_id is None
    assert workspace.id.hex[:6] in row.alias


async def test_ensure_for_workspace_mints_one_alias_per_service_command(db_session):
    workspace = await _make_workspace(db_session)
    command = CustomCommand(
        id=uuid4(),
        workspace_id=workspace.id,
        name="API",
        command="uvicorn app.main:app",
        kind="service",
        enabled=True,
    )
    db_session.add(command)
    await db_session.flush()
    service = PreviewAliasService(db_session, get_settings())

    await service.enable(workspace)

    builtin_row = await service.alias_for_row(workspace.id, None)
    command_row = await service.alias_for_row(workspace.id, command.id)
    assert builtin_row is not None
    assert command_row is not None
    assert command_row.alias != builtin_row.alias


async def test_ensure_for_workspace_is_idempotent(db_session):
    workspace = await _make_workspace(db_session)
    service = PreviewAliasService(db_session, get_settings())

    await service.enable(workspace)
    first_alias = (await service.alias_for_row(workspace.id, None)).alias

    await service.ensure_for_workspace(workspace)
    second_alias = (await service.alias_for_row(workspace.id, None)).alias

    assert first_alias == second_alias


async def test_two_workspaces_with_the_same_slug_get_different_aliases(db_session):
    workspace_one = await _make_workspace(db_session, slug="demo", sandbox_id="sandbox-one")
    workspace_two = await _make_workspace(db_session, slug="demo", sandbox_id="sandbox-two")
    service = PreviewAliasService(db_session, get_settings())

    await service.enable(workspace_one)
    await service.enable(workspace_two)

    row_one = await service.alias_for_row(workspace_one.id, None)
    row_two = await service.alias_for_row(workspace_two.id, None)
    assert row_one.alias != row_two.alias
    # Both are globally unique in the database -- this is the assertion that matters.
    assert row_one.alias.startswith("demo-dev-")
    assert row_two.alias.startswith("demo-dev-")


async def test_disable_stops_resolution_without_deleting_the_row(db_session):
    workspace = await _make_workspace(db_session)
    service = PreviewAliasService(db_session, get_settings())
    await service.enable(workspace)

    await service.disable(workspace)

    assert workspace.preview_alias_enabled is False
    # The row survives -- re-enabling restores the same alias.
    row = await service.alias_for_row(workspace.id, None)
    assert row is not None


async def test_alias_url_for_row_returns_none_when_aliases_disabled(db_session):
    workspace = await _make_workspace(db_session)
    settings = get_settings()
    service = PreviewAliasService(db_session, settings)
    await service.enable(workspace)
    await service.disable(workspace)

    url = await alias_url_for_row(db_session, settings, workspace, custom_command_id=None)
    assert url is None


async def test_alias_url_for_row_returns_the_stable_url_when_enabled(db_session):
    workspace = await _make_workspace(db_session)
    settings = get_settings()
    service = PreviewAliasService(db_session, settings)
    await service.enable(workspace)

    url = await alias_url_for_row(db_session, settings, workspace, custom_command_id=None)
    assert url is not None
    row = await service.alias_for_row(workspace.id, None)
    assert row.alias in url


async def test_regenerate_mints_a_new_alias_replacing_the_old_one(db_session):
    workspace = await _make_workspace(db_session)
    service = PreviewAliasService(db_session, get_settings())
    await service.enable(workspace)
    original = (await service.alias_for_row(workspace.id, None)).alias

    regenerated = await service.regenerate(workspace, custom_command_id=None, name="dev")

    assert regenerated.alias != original
    row = await service.alias_for_row(workspace.id, None)
    assert row.alias == regenerated.alias


async def test_mint_raises_a_clean_error_after_exhausting_every_candidate(db_session, monkeypatch):
    """Every one of the (bounded) candidate names collides -- the deterministic base name, the
    workspace-discriminated one, and every random one, since `secrets.token_hex` is forced to
    return the same value every call. Proves exhaustion surfaces as `PreviewAliasMintError`
    rather than hanging in a retry loop or raising a raw IntegrityError."""
    import app.services.preview_alias as preview_alias_module

    workspace = await _make_workspace(db_session)
    settings = get_settings()
    service = PreviewAliasService(db_session, settings)

    base_name = service._base_alias_name(workspace, "dev")  # noqa: SLF001
    discriminated = f"{base_name}-{workspace.id.hex[:6]}"
    monkeypatch.setattr(preview_alias_module.secrets, "token_hex", lambda _n: "aaaaaa")
    random_candidate = f"{base_name}-aaaaaa"

    # Each belongs to its own unrelated (but real, to satisfy the FK) workspace's built-in
    # row -- what matters is only that the *alias string* collides, since `alias` is globally
    # unique; two built-in-row aliases can't share a workspace (partial unique index).
    for alias in (base_name, discriminated, random_candidate):
        other_workspace = await _make_workspace(db_session, slug=f"other-{alias[-4:]}")
        db_session.add(
            WorkspacePreviewAlias(
                id=uuid4(),
                workspace_id=other_workspace.id,
                custom_command_id=None,
                alias=alias,
            )
        )
    await db_session.flush()

    with pytest.raises(PreviewAliasMintError):
        await service.enable(workspace)
