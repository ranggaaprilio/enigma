"""The process-local run state seam: ``RunRegistry`` / ``EventBus`` / ``RunLock``.

Every in-process registry (run tasks, stop events, providers, stream subscribers, the
per-workspace run guard) is reached through these interfaces so a distributed backend can be
swapped in later without touching call sites.
"""

import asyncio
import re
from pathlib import Path
from uuid import uuid4

from app.services.agents import AgentRuntime
from app.services.commands import CommandRuntime
from app.services.notifications import NotificationRuntime
from app.services.runtime import (
    EventBus,
    InMemoryEventBus,
    InMemoryRunLock,
    InMemoryRunRegistry,
    RunLock,
    RunRegistry,
)


class FakeProvider:
    def __init__(self) -> None:
        self.paused = False

    def pause(self) -> None:
        self.paused = True


def test_in_memory_implementations_satisfy_protocols():
    assert isinstance(InMemoryRunRegistry(), RunRegistry)
    assert isinstance(InMemoryEventBus(), EventBus)
    assert isinstance(InMemoryRunLock(), RunLock)


async def test_registry_tracks_task_until_done_then_forgets_it():
    registry = InMemoryRunRegistry()
    run_id = uuid4()
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())

    registry.register(run_id, task)
    assert registry.get_task(run_id) is task
    assert registry.active_tasks() == [task]

    release.set()
    await task
    await asyncio.sleep(0)  # let the done-callback run
    assert registry.get_task(run_id) is None
    assert registry.active_tasks() == []


async def test_registry_request_stop_sets_event_and_pauses_provider():
    registry = InMemoryRunRegistry()
    run_id = uuid4()
    stop_event = asyncio.Event()
    task = asyncio.create_task(stop_event.wait())
    provider = FakeProvider()

    registry.register(run_id, task, stop_event)
    registry.register_provider(run_id, provider)
    registry.request_stop(run_id)

    assert stop_event.is_set()
    assert provider.paused
    await task


async def test_registry_lists_the_ids_of_runs_still_in_flight():
    registry = InMemoryRunRegistry()
    live_id, done_id = uuid4(), uuid4()
    release = asyncio.Event()
    live = asyncio.create_task(release.wait())
    done = asyncio.create_task(asyncio.sleep(0))
    registry.register(live_id, live)
    registry.register(done_id, done)
    await done

    assert registry.active_run_ids() == [live_id]
    release.set()
    await live


def test_registry_request_stop_on_unknown_run_is_a_no_op():
    InMemoryRunRegistry().request_stop(uuid4())


async def test_event_bus_delivers_only_to_topic_subscribers_until_unsubscribed():
    bus = InMemoryEventBus()
    first = bus.subscribe("a")
    second = bus.subscribe("a")
    other = bus.subscribe("b")

    await bus.publish("a", {"n": 1})
    assert first.get_nowait() == {"n": 1}
    assert second.get_nowait() == {"n": 1}
    assert other.empty()

    bus.unsubscribe("a", first)
    await bus.publish("a", {"n": 2})
    assert first.empty()
    assert second.get_nowait() == {"n": 2}


def test_event_bus_unsubscribe_unknown_topic_is_a_no_op():
    bus = InMemoryEventBus()
    bus.unsubscribe("missing", asyncio.Queue())


async def test_run_lock_serialises_holders_of_the_same_key_only():
    lock = InMemoryRunLock()
    order: list[str] = []
    first_inside = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with lock.hold("ws-1"):
            order.append("first-in")
            first_inside.set()
            await release_first.wait()
            order.append("first-out")

    async def second() -> None:
        await first_inside.wait()
        async with lock.hold("ws-1"):
            order.append("second-in")

    async def unrelated() -> None:
        await first_inside.wait()
        async with lock.hold("ws-2"):
            order.append("unrelated-in")

    tasks = [asyncio.create_task(fn()) for fn in (first, second, unrelated)]
    await first_inside.wait()
    await asyncio.sleep(0.01)
    assert order == ["first-in", "unrelated-in"]

    release_first.set()
    await asyncio.gather(*tasks)
    assert order == ["first-in", "unrelated-in", "first-out", "second-in"]


async def test_run_lock_forgets_released_keys():
    lock = InMemoryRunLock()
    async with lock.hold("ws-1"):
        pass
    assert lock.held_keys() == set()


async def test_runtimes_sharing_one_bus_do_not_cross_deliver():
    """Agent runs, command runs and notifications share an EventBus; topics are namespaced so
    an id collision between domains can never leak a payload across streams."""
    bus = InMemoryEventBus()
    agents = AgentRuntime(bus=bus)
    commands = CommandRuntime(bus=bus)
    notifications = NotificationRuntime(bus=bus)
    shared_id = uuid4()

    agent_queue = agents.subscribe(shared_id)
    command_queue = commands.subscribe(shared_id)
    notification_queue = notifications.subscribe(shared_id)

    await agents.publish(shared_id, {"from": "agent"})
    await commands.publish(shared_id, {"from": "command"})
    await notifications.publish(shared_id, {"from": "notification"})

    assert agent_queue.get_nowait() == {"from": "agent"}
    assert command_queue.get_nowait() == {"from": "command"}
    assert notification_queue.get_nowait() == {"from": "notification"}
    assert agent_queue.empty() and command_queue.empty() and notification_queue.empty()


async def test_agent_and_command_runtimes_can_share_one_registry():
    registry = InMemoryRunRegistry()
    agents = AgentRuntime(registry=registry)
    commands = CommandRuntime(registry=registry)
    release = asyncio.Event()
    agent_task = asyncio.create_task(release.wait())
    command_task = asyncio.create_task(release.wait())

    agents.register_run(uuid4(), agent_task, asyncio.Event())
    commands.register_run(uuid4(), command_task)
    assert set(registry.active_tasks()) == {agent_task, command_task}

    release.set()
    await asyncio.gather(agent_task, command_task)


_APP_DIR = Path(__file__).resolve().parent.parent / "app"
# Container access on a registry attribute: indexing, a method call on it, or (re)binding it.
_REGISTRY_ATTRS = re.compile(r"\.(subscribers|stop_events|providers|tasks)(\[|\.|\s*[:=])")


def test_no_call_site_reaches_into_process_local_registries():
    """Acceptance: process-local registries are reached only through the runtime interfaces."""
    offenders = []
    for path in _APP_DIR.rglob("*.py"):
        if path.name == "runtime.py" and path.parent.name == "services":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _REGISTRY_ATTRS.search(line):
                offenders.append(f"{path.relative_to(_APP_DIR)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
