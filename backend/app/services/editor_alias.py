"""The editor's own proxy-host family: a stable alias for the embedded code-server.

Three shapes can now appear as the single DNS label under the preview proxy's hostname:

1. a 63-character base32 **capability** (`preview_capability.is_capability_label`),
2. an **editor alias** -- ``editor-`` plus 12 hex characters (this module), and
3. a service **preview alias** (`preview_alias.is_alias_label`).

(1) is disjoint from (2) and (3) by construction: `-` is not in the base32 alphabet. (2) is kept
disjoint from (3) by `preview_alias.validate_alias`, which refuses to mint anything matching
`is_editor_alias_label` -- so the proxy can dispatch on shape alone, with no ordering hazard and
no database lookup to decide which family a host belongs to.

The editor deliberately gets a *stable* host rather than a rotating capability: code-server keeps
its entire workbench state (open editors, layout, settings) in `localStorage`, which is keyed by
origin. A host that rotated hourly would reset the editor mid-session. Authorisation therefore
moves off the URL and onto the alias grant cookie that `preview_alias` already implements.
"""

from __future__ import annotations

import re
import secrets
from urllib.parse import quote, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Workspace
from app.services.preview_capability import parse_proxy_base_url

EDITOR_ALIAS_PREFIX = "editor-"

# 12 hex characters (48 bits) of randomness. Long enough that the label is not guessable, short
# enough to stay far below the 62-character alias ceiling. The fixed shape is what lets
# `validate_alias` exclude the whole family from user-mintable preview aliases.
_EDITOR_ALIAS_PATTERN = re.compile(r"^editor-[0-9a-f]{12}$")

_MAX_MINT_ATTEMPTS = 5


class EditorAliasMintError(Exception):
    pass


def is_editor_alias_label(label: str) -> bool:
    """Structural check only -- no database access, so the proxy can classify a host before it
    touches the connection pool."""
    return bool(_EDITOR_ALIAS_PATTERN.match(label))


def generate_editor_alias() -> str:
    return f"{EDITOR_ALIAS_PREFIX}{secrets.token_hex(6)}"


async def ensure_editor_alias(session: AsyncSession, workspace: Workspace) -> str:
    """Idempotent: returns the workspace's existing alias, or mints and assigns one.

    Retries on the (vanishingly unlikely) unique-constraint collision by checking the candidate
    against the table first; the column's UNIQUE constraint remains the real arbiter, and a lost
    race surfaces as an ``IntegrityError`` from the caller's commit rather than a silent reuse.
    """
    if workspace.editor_alias:
        return workspace.editor_alias
    for _attempt in range(_MAX_MINT_ATTEMPTS):
        candidate = generate_editor_alias()
        taken = await session.execute(
            select(Workspace.id).where(Workspace.editor_alias == candidate).limit(1)
        )
        if taken.scalar_one_or_none() is not None:
            continue
        workspace.editor_alias = candidate
        session.add(workspace)
        return candidate
    raise EditorAliasMintError("Could not mint a unique editor alias")


def editor_alias_url(alias: str, *, base_url: str, folder: str | None = None) -> str:
    """The stable editor URL. Carries no credential -- the grant cookie does that -- so it is
    safe to persist on the session row and show to the user."""
    parsed = parse_proxy_base_url(base_url)
    host = f"{alias}.{parsed.hostname}"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    base = urlunsplit((parsed.scheme, host, "/", "", ""))
    if folder is None:
        return base
    return f"{base}?folder={quote(folder, safe='/')}"


def editor_prime_url(
    alias: str,
    *,
    base_url: str,
    grant: str,
    capability_query_param: str,
    folder: str | None = None,
) -> str:
    """The URL the browser must load *first*: it carries a short-lived grant that the proxy
    exchanges for the host-scoped cookie. Every later request on this origin -- including
    code-server's WebSocket -- rides that cookie instead.

    The grant is minted fresh per API response and never stored, so this URL is the only
    credential-bearing form and it goes straight into the iframe's `src`.
    """
    base = editor_alias_url(alias, base_url=base_url, folder=folder)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{capability_query_param}={grant}"
