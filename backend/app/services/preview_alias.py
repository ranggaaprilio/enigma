"""Epic 13: stable per-service preview aliases -- an opt-in, weaker-posture hostname that
survives capability rotation, alongside the always-on rotating capability.

Three layers, each independently testable:

1. **Host classification** (`classify_proxy_host`) -- a single DNS label under the proxy suffix
   is either a 63-char base32 *capability* (`preview_capability.is_capability_label`) or an
   *alias* (`is_alias_label`), and the two shapes are mutually exclusive by construction: `-` is
   not in the base32 alphabet, and an alias is capped at 62 characters, so no alias can ever
   reach the capability's exact 63.
2. **The alias grant** -- a short-lived, signed, `HttpOnly` cookie minted the first time a
   request presents a valid capability for the alias's own workspace. Domain-separated from the
   platform session token (`core.security.create_session_token`) so a grant can never be replayed
   as a login session or vice versa, even though both are signed with the same `auth_secret_key`.
3. **Minting/lifecycle** (`PreviewAliasService`) -- aliases are keyed on `custom_command_id`
   (nullable; `None` = the built-in mise row), never on `workspace_services.id`, because
   `WorkspaceServiceManager._sync_definitions` deletes and recreates instance rows on every
   `list()`/`ensure_running()` call. A FK to that table would be silently orphaned the moment a
   workspace's registry changes -- the alias must outlive that churn, which is the whole point.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import CustomCommand, Workspace, WorkspacePreviewAlias
from app.services.preview_capability import (
    InvalidPreviewCapabilityError,
    is_capability_label,
    parse_proxy_base_url,
    proxy_host_label,
)
from app.services.utils import slugify

# ---------------------------------------------------------------------------
# Alias label validation
# ---------------------------------------------------------------------------

ALIAS_MIN_LENGTH = 3
ALIAS_MAX_LENGTH = 62

# Mirrors `ck_workspace_preview_aliases_alias_format` exactly: lowercase alphanumeric with
# hyphens in the middle, at least one hyphen (the property that guarantees disjointness from a
# 63-char, hyphen-free base32 capability label), never punycode.
_ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}[a-z0-9]$")

# Names that could be confused with platform infrastructure, or that a user could plausibly
# expect to resolve to something else. Not enforced by the database -- these are *shorter* than
# the mandatory-hyphen format allows in most cases, so an auto-generated (always hyphenated,
# workspace-discriminated) alias can never collide with one, but a defensive check costs nothing.
RESERVED_ALIAS_LABELS = frozenset(
    {
        "www",
        "api",
        "app",
        "admin",
        "preview",
        "proxy",
        "dashboard",
        "auth",
        "login",
        "logout",
        "static",
        "assets",
        "cdn",
        "mail",
        "smtp",
        "ns1",
        "ns2",
        "localhost",
        "well-known",
        "acme-challenge",
    }
)


class InvalidPreviewAliasError(Exception):
    pass


def is_alias_label(label: str) -> bool:
    """Structural check only -- format, hyphen requirement, no punycode. Matches the database
    CHECK constraint exactly (`ck_workspace_preview_aliases_alias_format`), so a label passing
    this always passes the DB too."""
    if not _ALIAS_PATTERN.match(label):
        return False
    if "-" not in label:
        return False
    if label.startswith("xn--"):
        return False
    return True


def validate_alias(candidate: str, *, base_url: str) -> str:
    """Full validation, including checks the database does not perform: the reserved-name list
    and collision with a label of the proxy's own hostname. Returns the candidate unchanged, or
    raises `InvalidPreviewAliasError` naming the problem."""
    if not is_alias_label(candidate):
        raise InvalidPreviewAliasError(f"{candidate!r} is not a valid alias label")
    if candidate in RESERVED_ALIAS_LABELS:
        raise InvalidPreviewAliasError(f"{candidate!r} is a reserved name")
    proxy_hostname = (urlsplit(base_url).hostname or "").lower()
    if candidate in proxy_hostname.split("."):
        raise InvalidPreviewAliasError(f"{candidate!r} collides with the proxy's own hostname")
    return candidate


# ---------------------------------------------------------------------------
# Host classification: alias vs. capability
# ---------------------------------------------------------------------------

ProxyHostKind = Literal["capability", "alias"]


@dataclass(frozen=True)
class ProxyHost:
    kind: ProxyHostKind
    label: str


def classify_proxy_host(host: str, *, base_url: str) -> ProxyHost:
    """Discriminate on shape alone, before any DB access, with no fallback between the two
    paths. Reuses `proxy_host_label`'s hardening (trailing dot, case, multi-label rejection)
    unmodified -- rather than duplicating it -- so alias resolution gets the exact same
    protections the capability path has always had."""
    label = proxy_host_label(host, base_url=base_url)
    if is_capability_label(label):
        return ProxyHost(kind="capability", label=label)
    if is_alias_label(label):
        return ProxyHost(kind="alias", label=label)
    raise InvalidPreviewCapabilityError("Invalid preview proxy host")


# ---------------------------------------------------------------------------
# Alias URLs
# ---------------------------------------------------------------------------


def alias_url(alias: str, *, base_url: str) -> str:
    """The stable URL for an alias -- safe to paste into an environment variable, unlike the
    rotating capability URL."""
    parsed = parse_proxy_base_url(base_url)
    host = f"{alias}.{parsed.hostname}"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, "/", "", ""))


def alias_prime_url(
    alias: str, *, base_url: str, capability: str, capability_query_param: str
) -> str:
    """A one-shot URL that primes the alias's grant cookie. Cookies are host-scoped, so serving
    a request on one alias can never set another alias's cookie -- the browser must visit (or be
    made to fetch) each alias at least once, carrying a capability for the *same workspace*,
    before it can call that alias without one. See `preview_proxy.py`'s gate."""
    base = alias_url(alias, base_url=base_url)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{capability_query_param}={capability}"


# ---------------------------------------------------------------------------
# The alias grant: a short-lived, signed cookie proving "this browser already presented a valid
# capability for this workspace", so the browser need not carry the (rotating, single-purpose)
# capability on every subsequent request to an alias.
# ---------------------------------------------------------------------------

ALIAS_GRANT_VERSION = 1
# Folded into the HMAC input (not the key) so a platform session token
# (`core.security.create_session_token`, signed with the same `auth_secret_key`) can never
# verify as a valid grant, and a grant can never verify as a platform session -- despite sharing
# a signing key, the two token *families* are cryptographically distinct by construction.
_ALIAS_GRANT_DOMAIN = b"enigma-preview-alias-grant-v1|"


class InvalidPreviewAliasGrantError(Exception):
    pass


@dataclass(frozen=True)
class AliasGrant:
    alias: str
    workspace_id: UUID
    issued_at: datetime
    expires_at: datetime


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def encode_alias_grant(
    *,
    alias: str,
    workspace_id: UUID,
    secret_key: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    payload = {
        "alias": alias,
        "exp": int(expires_at.timestamp()),
        "iat": int(issued_at.timestamp()),
        "typ": "preview-alias",
        "v": ALIAS_GRANT_VERSION,
        "ws": workspace_id.hex,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_segment = _b64_encode(payload_json)
    signature = hmac.new(
        secret_key.encode("utf-8"),
        _ALIAS_GRANT_DOMAIN + payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_segment}.{_b64_encode(signature)}"


def decode_alias_grant(
    value: str,
    *,
    alias: str,
    workspace_id: UUID,
    secret_key: str,
    max_lifetime_seconds: int,
    now: datetime | None = None,
) -> AliasGrant:
    """Verifies the signature, then binds the result to *this* request's alias and workspace --
    a grant minted for a different alias (even in the same workspace) or a different workspace
    (even the same alias string, freed and re-minted elsewhere) is rejected."""
    try:
        payload_segment, signature_segment = value.split(".", 1)
        expected_signature = hmac.new(
            secret_key.encode("utf-8"),
            _ALIAS_GRANT_DOMAIN + payload_segment.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64_encode(expected_signature), signature_segment):
            raise InvalidPreviewAliasGrantError("Invalid preview alias grant")
        payload = json.loads(_b64_decode(payload_segment))
        if payload.get("typ") != "preview-alias" or payload.get("v") != ALIAS_GRANT_VERSION:
            raise InvalidPreviewAliasGrantError("Invalid preview alias grant")
        issued_at = datetime.fromtimestamp(int(payload["iat"]), UTC)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
        grant_alias = str(payload["alias"])
        grant_workspace_id = UUID(hex=str(payload["ws"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidPreviewAliasGrantError("Invalid preview alias grant") from error

    if grant_alias != alias or grant_workspace_id != workspace_id:
        raise InvalidPreviewAliasGrantError("Preview alias grant does not match this request")

    current_time = now or datetime.now(UTC)
    if expires_at < current_time:
        raise InvalidPreviewAliasGrantError("Preview alias grant has expired")
    if (current_time - issued_at).total_seconds() > max_lifetime_seconds:
        raise InvalidPreviewAliasGrantError("Preview alias grant is past its absolute lifetime")

    return AliasGrant(
        alias=grant_alias,
        workspace_id=grant_workspace_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def should_refresh_grant(
    grant: AliasGrant, *, ttl_seconds: int, now: datetime | None = None
) -> bool:
    """Refresh only once under half the idle TTL remains, so a burst of subresource requests
    does not each carry a `Set-Cookie`."""
    current_time = now or datetime.now(UTC)
    remaining = (grant.expires_at - current_time).total_seconds()
    return remaining < (ttl_seconds / 2)


# ---------------------------------------------------------------------------
# Minting and lifecycle
# ---------------------------------------------------------------------------

_MAX_MINT_ATTEMPTS = 5


class PreviewAliasMintError(Exception):
    pass


class PreviewAliasService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def aliases_enabled(self, workspace: Workspace) -> bool:
        return bool(workspace.preview_alias_enabled)

    async def enable(self, workspace: Workspace) -> Workspace:
        workspace.preview_alias_enabled = True
        self.session.add(workspace)
        await self.ensure_for_workspace(workspace)
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def disable(self, workspace: Workspace) -> Workspace:
        """An all-at-once kill switch: existing `workspace_preview_aliases` rows are left in
        place (so re-enabling restores the same names), but resolution checks this flag first,
        so every alias stops resolving immediately."""
        workspace.preview_alias_enabled = False
        self.session.add(workspace)
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def ensure_for_workspace(self, workspace: Workspace) -> list[WorkspacePreviewAlias]:
        """Idempotent: mint an alias for the built-in row and every enabled `kind='service'`
        command that does not already have one. Called on enable, whenever a service command is
        created/enabled while aliases are already on, and defensively before every read."""
        existing = await self._existing_by_command_id(workspace.id)
        created: list[WorkspacePreviewAlias] = []

        if None not in existing:
            row = await self._mint(workspace, custom_command_id=None, name="dev")
            existing[None] = row
            created.append(row)

        result = await self.session.execute(
            select(CustomCommand).where(
                CustomCommand.workspace_id == workspace.id,
                CustomCommand.kind == "service",
                CustomCommand.enabled.is_(True),
            )
        )
        for command in result.scalars().all():
            if command.id in existing:
                continue
            row = await self._mint(workspace, custom_command_id=command.id, name=command.name)
            existing[command.id] = row
            created.append(row)

        if created:
            await self.session.commit()
        return created

    async def regenerate(
        self, workspace: Workspace, *, custom_command_id: UUID | None, name: str
    ) -> WorkspacePreviewAlias:
        """Mints a *new* alias string for this row, replacing the old one -- any grant or env
        var referencing the old alias breaks immediately. Aliases are otherwise immutable
        (renaming the command never changes its alias) precisely so callers don't need this.

        Unlike `_mint`'s normal (deterministic-first) candidate order -- which exists so
        `ensure_for_workspace` is idempotent -- this always forces a random candidate, or
        deleting-then-re-minting would silently hand back the exact same name.
        """
        existing = await self._get_alias_row(workspace.id, custom_command_id)
        if existing is not None:
            await self.session.delete(existing)
            await self.session.flush()
        row = await self._mint(
            workspace, custom_command_id=custom_command_id, name=name, force_random=True
        )
        await self.session.commit()
        return row

    async def alias_for_row(
        self, workspace_id: UUID, custom_command_id: UUID | None
    ) -> WorkspacePreviewAlias | None:
        return await self._get_alias_row(workspace_id, custom_command_id)

    async def _existing_by_command_id(
        self, workspace_id: UUID
    ) -> dict[UUID | None, WorkspacePreviewAlias]:
        result = await self.session.execute(
            select(WorkspacePreviewAlias).where(
                WorkspacePreviewAlias.workspace_id == workspace_id
            )
        )
        return {row.custom_command_id: row for row in result.scalars().all()}

    async def _get_alias_row(
        self, workspace_id: UUID, custom_command_id: UUID | None
    ) -> WorkspacePreviewAlias | None:
        result = await self.session.execute(
            select(WorkspacePreviewAlias).where(
                WorkspacePreviewAlias.workspace_id == workspace_id,
                WorkspacePreviewAlias.custom_command_id.is_(custom_command_id)
                if custom_command_id is None
                else WorkspacePreviewAlias.custom_command_id == custom_command_id,
            )
        )
        return result.scalars().first()

    def _candidate_names(
        self, base_name: str, workspace_id: UUID, *, force_random: bool = False
    ):
        if not force_random:
            yield base_name
            yield f"{base_name}-{workspace_id.hex[:6]}"
            remaining = _MAX_MINT_ATTEMPTS - 2
        else:
            remaining = _MAX_MINT_ATTEMPTS
        for _ in range(remaining):
            yield f"{base_name}-{secrets.token_hex(3)}"

    async def _mint(
        self,
        workspace: Workspace,
        *,
        custom_command_id: UUID | None,
        name: str,
        force_random: bool = False,
    ) -> WorkspacePreviewAlias:
        base_name = validate_alias(
            self._base_alias_name(workspace, name),
            base_url=self.settings.daytona_preview_proxy_base_url,
        )
        last_error: Exception | None = None
        for candidate in self._candidate_names(
            base_name, workspace.id, force_random=force_random
        ):
            try:
                async with self.session.begin_nested():
                    row = WorkspacePreviewAlias(
                        id=uuid4(),
                        workspace_id=workspace.id,
                        custom_command_id=custom_command_id,
                        alias=candidate,
                    )
                    self.session.add(row)
                    await self.session.flush()
                return row
            except IntegrityError as error:
                last_error = error
                continue
        raise PreviewAliasMintError(
            f"Could not mint a unique preview alias for {name!r} after "
            f"{_MAX_MINT_ATTEMPTS} attempts"
        ) from last_error

    def _base_alias_name(self, workspace: Workspace, name: str) -> str:
        """`<workspace-slug>-<service-name>-<workspace-id-prefix>`. The workspace-id suffix is
        unconditional (not just a collision fallback): `workspaces.slug` is only a plain,
        non-unique index (`ix_workspaces_slug`), so two workspaces can share a slug -- without
        the discriminator, a second workspace named "demo" could squat or collide with the
        first's "demo-api" alias."""
        slug_part = slugify(workspace.slug, fallback="ws")
        name_part = slugify(name, fallback="svc")
        discriminator = workspace.id.hex[:6]
        base = f"{slug_part}-{name_part}-{discriminator}"
        # Truncate from the middle pieces (never the discriminator, which guarantees
        # uniqueness) if the join runs past the 62-char cap; keep the format's edge
        # requirements (starts/ends with an alphanumeric) satisfied by trimming whole
        # hyphen-joined segments rather than mid-character.
        if len(base) > ALIAS_MAX_LENGTH:
            overflow = len(base) - ALIAS_MAX_LENGTH
            slug_part = slug_part[: max(1, len(slug_part) - overflow)]
            base = f"{slug_part}-{name_part}-{discriminator}"
        if len(base) > ALIAS_MAX_LENGTH:
            base = base[-ALIAS_MAX_LENGTH:]
            base = base.lstrip("-")
        return base


async def alias_url_for_row(
    session: AsyncSession,
    settings: Settings,
    workspace: Workspace,
    *,
    custom_command_id: UUID | None,
) -> str | None:
    """The stable alias URL for one service, or `None` when aliases are disabled for this
    workspace or no alias has been minted for this row yet. The seam the env-template engine
    (`${ENIGMA_PREVIEW_URL:<service>}`) and the composition endpoint both call through."""
    if not workspace.preview_alias_enabled:
        return None
    service = PreviewAliasService(session, settings)
    row = await service.alias_for_row(workspace.id, custom_command_id)
    if row is None or not row.enabled:
        return None
    return alias_url(row.alias, base_url=settings.daytona_preview_proxy_base_url)
