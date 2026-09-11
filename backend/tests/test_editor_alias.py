"""The editor's host family: label shape, minting, and URL construction.

The security-relevant property under test is *disjointness*. Three label families now share one
DNS position under the proxy hostname, and the proxy dispatches on shape alone -- so if any two
families could ever produce the same label, a host could be resolved by the wrong resolver with
the wrong authorisation rules. These tests pin that shut from both directions.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import User, Workspace
from app.services.editor_alias import (
    EDITOR_ALIAS_PREFIX,
    EditorAliasMintError,
    editor_alias_url,
    editor_prime_url,
    ensure_editor_alias,
    generate_editor_alias,
    is_editor_alias_label,
)
from app.services.preview_alias import (
    InvalidPreviewAliasError,
    is_alias_label,
    validate_alias,
)
from app.services.preview_capability import (
    encode_preview_capability,
    is_capability_label,
)

BASE_URL = "https://preview.example.com"


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    ["editor-a1b2c3d4e5f6", "editor-000000000000", "editor-ffffffffffff"],
)
def test_valid_editor_labels(label):
    assert is_editor_alias_label(label) is True


@pytest.mark.parametrize(
    "label",
    [
        "editor",  # no suffix
        "editor-",  # empty suffix
        "editor-a1b2c3d4e5f",  # 11 hex, too short
        "editor-a1b2c3d4e5f6a",  # 13 hex, too long
        "editor-A1B2C3D4E5F6",  # uppercase
        "editor-g1b2c3d4e5f6",  # non-hex
        "editors-a1b2c3d4e5f6",  # different prefix
        "my-editor-a1b2c3d4e5f6",  # prefixed
        "editor-a1b2c3d4e5f6-x",  # suffixed
    ],
)
def test_invalid_editor_labels(label):
    assert is_editor_alias_label(label) is False


def test_a_generated_editor_alias_is_always_a_valid_editor_label():
    for _ in range(50):
        alias = generate_editor_alias()
        assert alias.startswith(EDITOR_ALIAS_PREFIX)
        assert is_editor_alias_label(alias) is True


def test_a_generated_editor_alias_is_never_a_capability_label():
    """`-` is not in the base32 alphabet, so the two can never collide structurally."""
    for _ in range(50):
        assert is_capability_label(generate_editor_alias()) is False


def test_a_freshly_minted_capability_is_never_an_editor_label():
    encoded = encode_preview_capability(
        uuid4(),
        13337,
        expires_at=datetime(2026, 6, 12, 12, 0, tzinfo=UTC),
        secret_key="secret",
    )
    assert is_capability_label(encoded) is True
    assert is_editor_alias_label(encoded) is False


def test_an_editor_label_is_alias_shaped_which_is_why_it_must_be_dispatched_first():
    """This is the hazard the proxy's branch order exists for: an editor label *does* satisfy
    `is_alias_label` (it is lowercase, hyphenated and under 62 chars), so checking the preview
    alias family first would swallow every editor host."""
    assert is_alias_label("editor-a1b2c3d4e5f6") is True


def test_validate_alias_refuses_to_mint_anything_in_the_editor_family():
    with pytest.raises(InvalidPreviewAliasError):
        validate_alias("editor-a1b2c3d4e5f6", base_url=BASE_URL)


def test_validate_alias_still_accepts_an_editor_lookalike_that_is_not_the_editor_shape():
    # A user's service genuinely called "editor" gets a workspace-discriminated alias whose
    # suffix is 6 hex chars, not 12 -- outside the editor family, so it stays mintable.
    assert validate_alias("editor-a1b2c3", base_url=BASE_URL) == "editor-a1b2c3"


def test_the_bare_editor_label_is_reserved():
    with pytest.raises(InvalidPreviewAliasError):
        validate_alias("editor", base_url=BASE_URL)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def test_editor_alias_url_without_a_folder():
    assert (
        editor_alias_url("editor-a1b2c3d4e5f6", base_url=BASE_URL)
        == "https://editor-a1b2c3d4e5f6.preview.example.com/"
    )


def test_editor_alias_url_carries_the_folder_and_no_credential():
    url = editor_alias_url("editor-a1b2c3d4e5f6", base_url=BASE_URL, folder="/home/daytona/ws")
    assert url == "https://editor-a1b2c3d4e5f6.preview.example.com/?folder=/home/daytona/ws"
    assert "__enigma_cap" not in url


def test_editor_alias_url_preserves_the_proxy_port():
    assert editor_alias_url("editor-a1b2c3d4e5f6", base_url="http://localhost:8001") == (
        "http://editor-a1b2c3d4e5f6.localhost:8001/"
    )


def test_editor_prime_url_appends_the_grant_after_the_folder():
    url = editor_prime_url(
        "editor-a1b2c3d4e5f6",
        base_url=BASE_URL,
        grant="g.r.a.n.t",
        capability_query_param="__enigma_cap",
        folder="/home/daytona/ws",
    )
    assert url == (
        "https://editor-a1b2c3d4e5f6.preview.example.com/"
        "?folder=/home/daytona/ws&__enigma_cap=g.r.a.n.t"
    )


def test_editor_prime_url_without_a_folder_still_uses_a_question_mark():
    url = editor_prime_url(
        "editor-a1b2c3d4e5f6",
        base_url=BASE_URL,
        grant="g",
        capability_query_param="__enigma_cap",
    )
    assert url == "https://editor-a1b2c3d4e5f6.preview.example.com/?__enigma_cap=g"


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
        await session.rollback()
    await engine.dispose()


async def _workspace(session) -> Workspace:
    user = User(
        id=uuid4(),
        email=f"{uuid4().hex}@example.com",
        password_hash="x",
        role="owner",
        is_active=True,
    )
    session.add(user)
    await session.flush()
    workspace = Workspace(
        id=uuid4(),
        owner_id=user.id,
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id=f"sandbox-{uuid4().hex[:8]}",
        workspace_path="workspace",
        status="ready",
    )
    session.add(workspace)
    await session.flush()
    return workspace


async def test_ensure_editor_alias_mints_once_and_is_idempotent(db_session):
    workspace = await _workspace(db_session)

    first = await ensure_editor_alias(db_session, workspace)
    second = await ensure_editor_alias(db_session, workspace)

    assert is_editor_alias_label(first)
    assert first == second == workspace.editor_alias


async def test_two_workspaces_get_different_editor_aliases(db_session):
    one = await _workspace(db_session)
    two = await _workspace(db_session)

    assert await ensure_editor_alias(db_session, one) != await ensure_editor_alias(db_session, two)


async def test_mint_raises_a_clean_error_when_every_candidate_is_taken(db_session, monkeypatch):
    workspace = await _workspace(db_session)
    other = await _workspace(db_session)
    other.editor_alias = "editor-aaaaaaaaaaaa"
    await db_session.flush()

    monkeypatch.setattr(
        "app.services.editor_alias.generate_editor_alias",
        lambda: "editor-aaaaaaaaaaaa",
    )

    with pytest.raises(EditorAliasMintError):
        await ensure_editor_alias(db_session, workspace)
