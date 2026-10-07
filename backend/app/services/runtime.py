"""The seam between request-path code and process-local run state.

Everything a single backend process keeps in memory about in-flight work is reached through one
of three interfaces:

- ``RunRegistry`` -- the asyncio task, stop event and provider driving each run (agent runs,
  command runs, and Spec Kit phase finalizers keyed by phase id).
- ``EventBus``    -- topic fan-out from a run (or a user's notifications) to stream subscribers.
- ``RunLock``     -- mutual exclusion keyed by an arbitrary string (e.g. "one run per workspace").

Only in-memory implementations exist today, so behaviour is exactly that of the previous
per-domain dicts. A distributed backend (Redis pub/sub, DB leases, advisory locks) can be swapped
in behind the same interfaces without touching call sites.

Deliberately *not* behind this seam: ``openhands_tools._BRIDGES``. Each bridge holds the event
loop of the run that owns it so OpenHands' worker thread can call back into it; it lives and
dies with that one run's task and is never shared, so there is nothing to distribute.
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol, runtime_checkable
from uuid import UUID


class Pausable(Protocol):
    def pause(self) -> None: ...


@runtime_checkable
class RunRegistry(Protocol):
    def register(
        self, run_id: UUID, task: asyncio.Task, stop_event: asyncio.Event | None = None
    ) -> None: ...

    def register_provider(self, run_id: UUID, provider: Pausable) -> None: ...

    def request_stop(self, run_id: UUID) -> None: ...

    def get_task(self, run_id: UUID) -> asyncio.Task | None: ...

    def active_tasks(self) -> list[asyncio.Task]: ...

    def active_run_ids(self) -> list[UUID]: ...

    def forget(self, run_id: UUID) -> None: ...


@runtime_checkable
class EventBus(Protocol):
    def subscribe(self, topic: str) -> asyncio.Queue[dict]: ...

    def unsubscribe(self, topic: str, queue: asyncio.Queue[dict]) -> None: ...

    async def publish(self, topic: str, payload: dict) -> None: ...


@runtime_checkable
class RunLock(Protocol):
    def hold(self, key: str) -> AbstractAsyncContextManager[None]: ...


class InMemoryRunRegistry:
    def __init__(self) -> None:
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._stop_events: dict[UUID, asyncio.Event] = {}
        self._providers: dict[UUID, Pausable] = {}

    def register(
        self, run_id: UUID, task: asyncio.Task, stop_event: asyncio.Event | None = None
    ) -> None:
        self._tasks[run_id] = task
        if stop_event is not None:
            self._stop_events[run_id] = stop_event
        task.add_done_callback(lambda _task: self.forget(run_id))

    def register_provider(self, run_id: UUID, provider: Pausable) -> None:
        self._providers[run_id] = provider

    def request_stop(self, run_id: UUID) -> None:
        if stop_event := self._stop_events.get(run_id):
            stop_event.set()
        if provider := self._providers.get(run_id):
            provider.pause()

    def get_task(self, run_id: UUID) -> asyncio.Task | None:
        return self._tasks.get(run_id)

    def active_tasks(self) -> list[asyncio.Task]:
        return [task for task in self._tasks.values() if not task.done()]

    def active_run_ids(self) -> list[UUID]:
        return [run_id for run_id, task in self._tasks.items() if not task.done()]

    def forget(self, run_id: UUID) -> None:
        self._tasks.pop(run_id, None)
        self._stop_events.pop(run_id, None)
        self._providers.pop(run_id, None)


class InMemoryEventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict]]] = defaultdict(set)

    def subscribe(self, topic: str) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribers[topic].add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue[dict]) -> None:
        queues = self._subscribers.get(topic)
        if not queues:
            return
        queues.discard(queue)
        if not queues:
            self._subscribers.pop(topic, None)

    async def publish(self, topic: str, payload: dict) -> None:
        for queue in list(self._subscribers.get(topic, set())):
            await queue.put(payload)


class InMemoryRunLock:
    """Per-key ``asyncio.Lock``s, dropped once nobody holds or waits on them."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._users: dict[str, int] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                del self._users[key]
                del self._locks[key]

    def held_keys(self) -> set[str]:
        return set(self._locks)


# Process-wide defaults. Agent and command runs share one registry so shutdown can drain every
# in-flight run from a single place; topics on the shared bus are namespaced per domain.
run_registry = InMemoryRunRegistry()
event_bus = InMemoryEventBus()
run_lock = InMemoryRunLock()
