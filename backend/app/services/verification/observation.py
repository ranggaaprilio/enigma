"""Observation interface for the Verifiable Agent Loop.

Captures behavioral evidence of a running app: requests the preview link from the
sandbox, fetches the page to build a structured snapshot (raw HTML + page metadata) and a
short human-readable description for the agent, and records a screenshot artifact
reference for the human reviewer. The result is a ``VerificationEvidence(kind="observation")``
row attached to the given ``VerificationRun``.

A preview failure (a ``SandboxRuntimeError`` from ``preview_link``) — or a page that
resolves but is not reachable (an ``httpx`` error while fetching) — never raises: it
records ``VerificationEvidence(kind="observation", status="observation_failed")`` and
returns, so the loop continues on test evidence.
"""

from __future__ import annotations

import re
from html import unescape

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_maker
from app.models import VerificationEvidence, VerificationRun, Workspace
from app.services.sandbox import (
    RuntimePreviewLink,
    SandboxRuntime,
    SandboxRuntimeError,
    get_sandbox_runtime,
)

logger = structlog.get_logger(__name__)

OBSERVATION_KIND = "observation"
OBSERVE_TIMEOUT_SECONDS = 10.0
SNAPSHOT_HTML_CAP = 64_000

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


async def observe(
    workspace: Workspace,
    run: VerificationRun,
    port: int,
    *,
    session: AsyncSession | None = None,
    runtime: SandboxRuntime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> VerificationEvidence:
    """Capture a structured snapshot + description + screenshot reference for ``port``.

    Requests the preview link from the sandbox, fetches the page to build a structured
    snapshot (HTML + page metadata) and a short human-readable description, and records a
    screenshot artifact reference for the human reviewer. The result is persisted as a new
    ``VerificationEvidence(kind="observation")`` on ``run`` and returned.

    ``preview_link`` failing (or the page being unreachable) records ``observation_failed``
    and returns — it never raises, so the loop continues on test evidence.

    ``runtime`` and ``session`` default to the process sandbox dispatcher and a fresh
    session from ``async_session_maker``; ``transport`` lets a test stub the preview page
    fetch (mirroring ``GitHubClient``).
    """
    runtime = runtime or get_sandbox_runtime()

    # Best effort: make the listening port reachable before we fetch it (a Docker sandbox
    # relays a loopback-only dev server; Daytona/OpenSandbox need nothing). Never raises.
    try:
        await runtime.expose_port(workspace, port)
    except SandboxRuntimeError as error:
        logger.warning(
            "observation_expose_port_failed",
            workspace_id=str(workspace.id),
            port=port,
            error_type=type(error).__name__,
        )

    try:
        link = await runtime.preview_link(workspace, port)
    except SandboxRuntimeError as error:
        return await _record(
            run,
            status="observation_failed",
            detail={"port": port, "error": str(error)},
            session=session,
        )

    try:
        snapshot, description = await _capture_snapshot(link, transport=transport)
    except httpx.HTTPError as error:
        return await _record(
            run,
            status="observation_failed",
            detail={"port": port, "url": link.url, "error": str(error)},
            session=session,
        )

    screenshot = _screenshot_ref(run, port)
    return await _record(
        run,
        status="passed",
        detail={
            "port": port,
            "url": link.url,
            "description": description,
            "snapshot": snapshot,
            "screenshot": screenshot,
        },
        path=screenshot,
        session=session,
    )


async def _capture_snapshot(
    link: RuntimePreviewLink,
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[dict, str]:
    """Fetch the preview page and build a structured snapshot + short description.

    The snapshot is the raw HTML (capped) plus page metadata; a full DOM walk or
    headless-browser capture is out of scope for this interface.
    """
    async with httpx.AsyncClient(
        timeout=OBSERVE_TIMEOUT_SECONDS, transport=transport
    ) as client:
        response = await client.get(link.url, follow_redirects=True)

    text = response.text
    title = _extract_title(text)
    snapshot = {
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "title": title,
        "html": text[:SNAPSHOT_HTML_CAP],
    }
    description = _describe(snapshot, link.url)
    return snapshot, description


def _extract_title(text: str) -> str | None:
    match = _TITLE_RE.search(text)
    if match is None:
        return None
    return unescape(match.group(1)).strip() or None


def _describe(snapshot: dict, url: str) -> str:
    content_type = snapshot["content_type"] or "unknown content type"
    title = snapshot["title"]
    suffix = f" titled {title!r}" if title else ""
    return f"Served HTTP {snapshot['status_code']} ({content_type}) at {url}{suffix}"


def _screenshot_ref(run: VerificationRun, port: int) -> str:
    """A stable artifact reference the human evidence view resolves to the captured image."""
    return f"verification/{run.id}/{port}.png"


async def _record(
    run: VerificationRun,
    *,
    status: str,
    detail: dict,
    session: AsyncSession | None,
    path: str | None = None,
) -> VerificationEvidence:
    evidence = VerificationEvidence(
        run_id=run.id, kind=OBSERVATION_KIND, status=status, detail=detail, path=path
    )
    if session is not None:
        session.add(evidence)
        await session.flush()
        await session.commit()
        await session.refresh(evidence)
    else:
        async with async_session_maker() as new_session:
            new_session.add(evidence)
            await new_session.commit()
            await new_session.refresh(evidence)

    logger.info(
        "verification_observation_recorded",
        run_id=str(run.id),
        port=detail.get("port"),
        status=status,
    )
    return evidence
