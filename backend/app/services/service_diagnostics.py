"""Epic 13: CORS diagnosis storage, recorded by the preview proxy.

The proxy sees every proxied request/response, so a naive "one DB write per response" design
would add a write on the hot path of every cross-origin call -- unacceptable. Instead:

- `ServiceDiagnosticsRecorder.record()` is synchronous, O(1), and never awaits or raises: a
  per-key in-process throttle guards a bounded `asyncio.Queue`, and a full queue just drops the
  event (diagnostics are best-effort; they must never add latency to, or fail, a proxied
  response).
- A single consumer coroutine (`run()`, started once in the proxy's `lifespan`) drains the queue
  in small batches, collapses duplicate keys in memory, and applies one upsert per key per
  batch -- so N identical CORS rejections in a burst cost one write, not N.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.session import async_session_maker
from app.models import ServiceDiagnostic

# How many events one batch applies at most, and how long the consumer waits for the first
# event of a new batch before looping (so shutdown/cancellation is responsive even when idle).
_BATCH_MAX_EVENTS = 50
_BATCH_WAIT_SECONDS = 1.0

DiagnosticKey = tuple[UUID, UUID, str, str]


@dataclass(frozen=True)
class DiagnosticEvent:
    """One observation from the proxy. `resolve=True` means "this origin/kind now looks fine,
    clear any standing diagnosis" rather than "record a new occurrence"."""

    workspace_id: UUID
    service_id: UUID
    kind: str
    origin: str
    preview_session_id: UUID | None = None
    request_method: str | None = None
    required_header: str | None = None
    request_path: str | None = None
    status_code: int | None = None
    detail: str | None = None
    resolve: bool = False

    @property
    def key(self) -> DiagnosticKey:
        return (self.workspace_id, self.service_id, self.kind, self.origin)


class ServiceDiagnosticsRecorder:
    def __init__(
        self,
        settings: Settings,
        session_factory: Callable[[], AsyncSession] = async_session_maker,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._queue: asyncio.Queue[DiagnosticEvent] = asyncio.Queue(
            maxsize=settings.service_diagnostic_queue_maxsize
        )
        # Last time (monotonic) a *new-occurrence* event for this key was enqueued -- a resolve
        # event always bypasses the throttle, since clearing a diagnosis promptly matters more
        # than batching it.
        self._last_enqueued: dict[DiagnosticKey, float] = {}
        self._task: asyncio.Task[None] | None = None

    def record(self, event: DiagnosticEvent) -> None:
        """Called from the request-handling path. Never awaits, never raises."""
        if not event.resolve:
            now = time.monotonic()
            last = self._last_enqueued.get(event.key)
            if (
                last is not None
                and (now - last) < self.settings.service_diagnostic_throttle_seconds
            ):
                return
            self._last_enqueued[event.key] = now
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run(self) -> None:
        """The single consumer loop. Runs until cancelled (`stop()`)."""
        while True:
            batch = await self._collect_batch()
            if batch:
                await self._apply_batch(batch)

    async def flush(self) -> None:
        """Test seam: drain and apply whatever is currently queued, synchronously, without the
        infinite loop or its wait timeout."""
        batch: list[DiagnosticEvent] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._apply_batch(batch)

    async def _collect_batch(self) -> list[DiagnosticEvent]:
        events: list[DiagnosticEvent] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=_BATCH_WAIT_SECONDS)
        except TimeoutError:
            return events
        events.append(first)
        while len(events) < _BATCH_MAX_EVENTS:
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    async def _apply_batch(self, events: list[DiagnosticEvent]) -> None:
        # Collapse in memory: the last action seen per key wins ("resolve" after a fresh
        # occurrence un-resolves it again), and occurrences count only the non-resolve events.
        pending: dict[DiagnosticKey, dict] = {}
        for event in events:
            state = pending.setdefault(event.key, {"count": 0, "resolve": False})
            if event.resolve:
                state["resolve"] = True
            else:
                state["count"] += 1
                state["resolve"] = False
            state["latest"] = event

        async with self.session_factory() as session:
            touched_workspaces: set[UUID] = set()
            for key, state in pending.items():
                touched_workspaces.add(key[0])
                latest: DiagnosticEvent = state["latest"]
                if state["resolve"]:
                    await self._apply_resolve(session, latest)
                elif state["count"] > 0:
                    await self._apply_upsert(session, latest, occurrences=state["count"])
            for workspace_id in touched_workspaces:
                await self._apply_retention(session, workspace_id)
            await session.commit()

    async def _apply_upsert(
        self, session: AsyncSession, event: DiagnosticEvent, *, occurrences: int
    ) -> None:
        now = datetime.now(UTC)
        stmt = insert(ServiceDiagnostic).values(
            id=uuid4(),
            workspace_id=event.workspace_id,
            service_id=event.service_id,
            preview_session_id=event.preview_session_id,
            kind=event.kind,
            origin=event.origin,
            request_method=event.request_method,
            required_header=event.required_header,
            request_path=event.request_path,
            status_code=event.status_code,
            detail=event.detail,
            occurrences=occurrences,
            first_seen_at=now,
            last_seen_at=now,
            resolved_at=None,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_service_diagnostics_service_kind_origin",
            set_={
                "occurrences": ServiceDiagnostic.occurrences + occurrences,
                "last_seen_at": now,
                "preview_session_id": event.preview_session_id,
                "request_method": event.request_method,
                "required_header": event.required_header,
                "request_path": event.request_path,
                "status_code": event.status_code,
                "detail": event.detail,
                "resolved_at": None,
            },
        )
        await session.execute(stmt)

    async def _apply_resolve(self, session: AsyncSession, event: DiagnosticEvent) -> None:
        now = datetime.now(UTC)
        await session.execute(
            update(ServiceDiagnostic)
            .where(
                ServiceDiagnostic.workspace_id == event.workspace_id,
                ServiceDiagnostic.service_id == event.service_id,
                ServiceDiagnostic.kind == event.kind,
                ServiceDiagnostic.origin == event.origin,
                ServiceDiagnostic.resolved_at.is_(None),
            )
            .values(resolved_at=now)
        )

    async def _apply_retention(self, session: AsyncSession, workspace_id: UUID) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=self.settings.service_diagnostic_retention_days)
        await session.execute(
            delete(ServiceDiagnostic).where(
                ServiceDiagnostic.workspace_id == workspace_id,
                ServiceDiagnostic.last_seen_at < cutoff,
            )
        )


class ServiceDiagnosticsService:
    """Read side, used by the composition endpoint -- never by the proxy itself."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def for_workspace(
        self, workspace_id: UUID, *, since: datetime | None = None
    ) -> list[ServiceDiagnostic]:
        stmt = select(ServiceDiagnostic).where(
            ServiceDiagnostic.workspace_id == workspace_id,
            ServiceDiagnostic.resolved_at.is_(None),
        )
        if since is not None:
            stmt = stmt.where(ServiceDiagnostic.last_seen_at >= since)
        stmt = stmt.order_by(ServiceDiagnostic.last_seen_at.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
