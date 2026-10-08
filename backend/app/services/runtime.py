"""The seam between request-path code and process-local run state.

Everything a single backend process keeps in memory about in-flight work is reached through one
of three interfaces:

- ``RunRegistry`` -- the asyncio task, stop event and provider driving each run (agent runs,
  command runs, and Spec Kit phase finalizers keyed by phase id).
- ``EventBus``    -- topic fan-out from a run (or a user's notifications) to stream subscribers.
- ``RunLock``     -- mutual exclusion keyed by an arbitrary string (e.g. "one run per workspace").

The in-memory implementations serve ``RUN_EXECUTION_MODE=inline`` (one process does
everything). In ``queue`` mode runs execute on worker processes (Epic 02), so the bus is
``RedisEventBus``: a worker publishes run events and an API replica's WebSocket receives them,
and stop signals travel the other way. The registry stays process-local in both modes -- a
worker registers the runs *it* executes -- and the lock is backed by the partial unique index.

Deliberately *not* behind this seam: ``openhands_tools._BRIDGES``. Each bridge holds the event
loop of the run that owns it so OpenHands' worker thread can call back into it; it lives and
dies with that one run's task and is never shared, so there is nothing to distribute.
"""

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Protocol, runtime_checkable
from uuid import UUID

import structlog
from redis.asyncio import Redis

from app.core.config import get_settings

logger = structlog.get_logger(__name__)


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

    async def wait_ready(self, topic: str) -> bool:
        """Wait until a ``subscribe`` is live, so a publish from another process is delivered."""
        ...


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

    async def wait_ready(self, topic: str) -> bool:
        return True


class RedisEventBus:
    """``EventBus`` over Redis pub/sub, for when publisher and subscriber are different processes.

    One pub/sub connection per process and one reader task fan messages out to local queues, so a
    WebSocket costs a queue, not a Redis connection. ``subscribe`` stays synchronous (as the
    protocol requires): it registers the queue and schedules the SUBSCRIBE; ``wait_ready`` then
    blocks until Redis confirms it. Delivery is at-most-once -- anything that must not be lost
    is persisted first (agent events, command output) and re-read by the subscriber.
    """

    CHANNEL_PREFIX = "enigma:bus:"
    READY_TIMEOUT_SECONDS = 5.0
    SUBSCRIBE_RETRY_SECONDS = 1.0

    def __init__(self, client: Redis) -> None:
        self._client = client
        self._pubsub = client.pubsub()
        self._local_queues: dict[str, set[asyncio.Queue[dict]]] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._subscribed: set[str] = set()
        self._sync_lock: asyncio.Lock | None = None
        self._reader: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()

    def _channel(self, topic: str) -> str:
        return f"{self.CHANNEL_PREFIX}{topic}"

    def _topic(self, channel: str) -> str:
        return channel.removeprefix(self.CHANNEL_PREFIX)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def subscribe(self, topic: str) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        queues = self._local_queues.setdefault(topic, set())
        if not queues:
            self._ready[topic] = asyncio.Event()
            self._spawn(self._sync_subscription(topic))
        queues.add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue[dict]) -> None:
        queues = self._local_queues.get(topic)
        if not queues:
            return
        queues.discard(queue)
        if not queues:
            self._local_queues.pop(topic, None)
            self._ready.pop(topic, None)
            self._spawn(self._sync_subscription(topic))

    async def _sync_subscription(self, topic: str) -> None:
        """Bring the Redis subscription for ``topic`` in line with local demand.

        Serialised, and decided from the *current* demand rather than the call that scheduled it,
        so a fast unsubscribe/resubscribe can never leave the two out of step.
        """
        if self._sync_lock is None:
            self._sync_lock = asyncio.Lock()
        async with self._sync_lock:
            wanted = topic in self._local_queues
            try:
                if wanted and topic not in self._subscribed:
                    await self._pubsub.subscribe(self._channel(topic))
                    self._subscribed.add(topic)
                    self._ensure_reader()
                elif wanted:
                    self._ready[topic].set()
                elif topic in self._subscribed:
                    await self._pubsub.unsubscribe(self._channel(topic))
                    self._subscribed.discard(topic)
            except Exception:
                logger.exception("event_bus_subscription_failed", topic=topic)
                if wanted:
                    # Later subscribers to this topic would not schedule a sync of their own.
                    self._spawn(self._retry_subscription(topic))

    async def _retry_subscription(self, topic: str) -> None:
        await asyncio.sleep(self.SUBSCRIBE_RETRY_SECONDS)
        await self._sync_subscription(topic)

    def _ensure_reader(self) -> None:
        if self._reader is None or self._reader.done():
            self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        while True:
            try:
                message = await self._pubsub.get_message(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # redis-py reconnects and re-subscribes on the next read; don't spin meanwhile.
                logger.exception("event_bus_read_failed")
                await asyncio.sleep(1.0)
                continue
            if message is None:
                continue
            topic = self._topic(message["channel"])
            if message["type"] == "subscribe":
                if event := self._ready.get(topic):
                    event.set()
            elif message["type"] == "message":
                try:
                    payload = json.loads(message["data"])
                except (TypeError, ValueError):
                    logger.warning("event_bus_bad_payload", topic=topic)
                    continue
                for queue in list(self._local_queues.get(topic, ())):
                    queue.put_nowait(payload)

    async def wait_ready(self, topic: str) -> bool:
        event = self._ready.get(topic)
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), self.READY_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("event_bus_subscribe_timeout", topic=topic)
            return False
        return True

    async def publish(self, topic: str, payload: dict) -> None:
        # Best effort, like delivery itself: callers publish *after* persisting, and a Redis blip
        # must never fail the run doing the publishing. Subscribers recover from Postgres.
        try:
            await self._client.publish(self._channel(topic), json.dumps(payload, default=str))
        except Exception:
            logger.warning("event_bus_publish_failed", topic=topic, exc_info=True)

    async def aclose(self) -> None:
        tasks = [*self._background, *([self._reader] if self._reader else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._reader = None
        with suppress(Exception):
            await self._pubsub.aclose()
        with suppress(Exception):
            await self._client.aclose()


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


def _default_event_bus() -> EventBus:
    settings = get_settings()
    if settings.run_execution_mode == "queue":
        return RedisEventBus(Redis.from_url(settings.redis_url, decode_responses=True))
    return InMemoryEventBus()


# Process-wide defaults. Agent and command runs share one registry so shutdown can drain every
# in-flight run from a single place; topics on the shared bus are namespaced per domain.
run_registry = InMemoryRunRegistry()
event_bus = _default_event_bus()
run_lock = InMemoryRunLock()
