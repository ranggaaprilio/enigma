"""Epic 03: backfill-then-tail streaming that survives replica hops and an at-most-once bus."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import fakeredis

from app.models import AgentEvent
from app.services.agents import AgentService
from tests.test_job_queue import db_session, queue_settings
from tests.test_worker_jobs import seed_run

__all__ = ["db_session"]

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def event_at(run_id, content: str, seconds: int) -> AgentEvent:
    return AgentEvent(
        agent_run_id=run_id,
        event_type="output_text",
        content=content,
        created_at=T0 + timedelta(seconds=seconds),
    )


# ---------------------------------------------------------------------------
# Agent events: ordered, incremental backfill
# ---------------------------------------------------------------------------


async def test_list_events_is_ordered_by_creation_time_whatever_the_insert_order(db_session):
    session, session_maker = db_session
    run = await seed_run(session, status="executing")
    session.add_all([event_at(run.id, "c", 3), event_at(run.id, "a", 1), event_at(run.id, "b", 2)])
    await session.commit()

    service = AgentService(session=session, session_maker=session_maker, settings=queue_settings())
    events = await service.list_events(run.workspace_id, run.id, run.user_id)

    assert [event.content for event in events] == ["a", "b", "c"]


async def test_list_events_since_returns_only_events_from_that_instant_on(db_session):
    session, session_maker = db_session
    run = await seed_run(session, status="executing")
    session.add_all([event_at(run.id, "a", 1), event_at(run.id, "b", 2), event_at(run.id, "c", 3)])
    await session.commit()

    service = AgentService(session=session, session_maker=session_maker, settings=queue_settings())
    events = await service.list_events(
        run.workspace_id, run.id, run.user_id, since=T0 + timedelta(seconds=2)
    )

    # Inclusive: an event sharing the high-water timestamp may not have been sent yet.
    assert [event.content for event in events] == ["b", "c"]


async def test_tick_catch_up_reads_only_from_the_high_water_mark(db_session):
    from app.api.agents import catch_up

    session, session_maker = db_session
    run = await seed_run(session, status="executing")
    old, new = event_at(run.id, "old", 1), event_at(run.id, "new", 5)
    unsent_but_older = event_at(run.id, "late", 0)
    session.add_all([old, new, unsent_but_older])
    await session.commit()

    payloads, ended = await catch_up(
        session_maker,
        queue_settings(),
        run.workspace_id,
        run.id,
        run.user_id,
        {str(old.id)},
        since=T0 + timedelta(seconds=1),
    )

    assert not ended
    assert [payload["content"] for payload in payloads] == ["new"]


async def test_catch_up_of_an_ended_run_rereads_everything(db_session):
    """A late commit with an older timestamp (e.g. stop on another replica) is not lost."""
    from app.api.agents import catch_up

    session, session_maker = db_session
    run = await seed_run(session, status="stopped", completed_at=datetime.now(UTC))
    sent = event_at(run.id, "sent", 5)
    late = event_at(run.id, "late", 1)
    session.add_all([sent, late])
    await session.commit()

    payloads, ended = await catch_up(
        session_maker,
        queue_settings(),
        run.workspace_id,
        run.id,
        run.user_id,
        {str(sent.id)},
        since=T0 + timedelta(seconds=5),
    )

    assert ended
    assert [payload["content"] for payload in payloads] == ["late"]


# ---------------------------------------------------------------------------
# Command output: offsets, gap fill, lost status frame
# ---------------------------------------------------------------------------


class ScriptedQueue:
    """Each ``get`` runs the next step: it may persist state, then returns the live frame."""

    def __init__(self, steps) -> None:
        self.steps = list(steps)

    async def get(self) -> dict:
        return await self.steps.pop(0)()


class ScriptedRuntime:
    def __init__(self, steps) -> None:
        self.queue = ScriptedQueue(steps)
        self.subscribed = False

    def subscribe(self, run_id):
        self.subscribed = True
        return self.queue

    def unsubscribe(self, run_id, queue) -> None:
        self.subscribed = False

    async def wait_ready(self, run_id) -> bool:
        return True


async def tail(session_maker, runtime, run, idle_seconds: float = 5.0) -> list[dict]:
    from app.api.commands import tail_command_run

    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    await tail_command_run(
        send,
        runtime,
        session_maker,
        queue_settings(),
        run.workspace_id,
        run.id,
        run.user_id,
        idle_seconds=idle_seconds,
    )
    return frames


async def seed_command_run(session, output: str, **fields):
    from tests.test_worker_jobs import seed_command_run as seed

    run = await seed(session, **fields)
    run.output = output
    session.add(run)
    await session.commit()
    return run


def output_of(frames: list[dict]) -> str:
    return "".join(frame["content"] for frame in frames if frame["type"] == "output")


async def test_command_stream_dedupes_overlap_and_refills_a_lost_chunk(db_session):
    session, session_maker = db_session
    run = await seed_command_run(session, output="ab")

    async def persist(output: str, **fields) -> None:
        run.output = output
        for name, value in fields.items():
            setattr(run, name, value)
        session.add(run)
        await session.commit()

    async def duplicate():  # published before the socket's first read; already sent
        return {"type": "output", "content": "ab", "offset": 0}

    async def next_chunk():
        await persist("abcd")
        return {"type": "output", "content": "cd", "offset": 2}

    async def chunk_after_a_lost_one():
        await persist("abcdefgh")  # "ef" was published but dropped by the bus
        return {"type": "output", "content": "gh", "offset": 6}

    async def finished():
        await persist("abcdefgh", status="completed", exit_code=0)
        return {"type": "status", "status": "completed", "exit_code": 0}

    runtime = ScriptedRuntime([duplicate, next_chunk, chunk_after_a_lost_one, finished])
    frames = await tail(session_maker, runtime, run)

    assert output_of(frames) == "abcdefgh"
    assert frames[-1] == {"type": "status", "status": "completed", "exit_code": 0}
    assert not runtime.subscribed


async def test_command_stream_ends_even_if_the_status_frame_is_lost(db_session):
    session, session_maker = db_session
    run = await seed_command_run(session, output="x")

    async def finishes_silently():
        run.output, run.status, run.exit_code = "xy", "failed", 2
        session.add(run)
        await session.commit()
        await asyncio.sleep(60)  # neither the "y" chunk nor the status frame ever arrive

    frames = await tail(session_maker, ScriptedRuntime([finishes_silently]), run, idle_seconds=0.05)

    assert output_of(frames) == "xy"
    assert frames[-1] == {"type": "status", "status": "failed", "exit_code": 2}


async def test_command_stream_of_a_run_that_ended_before_subscribing(db_session):
    session, session_maker = db_session
    run = await seed_command_run(session, status="completed", output="done", exit_code=0)

    frames = await tail(session_maker, ScriptedRuntime([]), run)

    assert frames == [
        {"type": "output", "content": "done"},
        {"type": "status", "status": "completed", "exit_code": 0},
    ]


async def test_append_output_publishes_the_offset_of_what_it_persisted(tmp_path):
    from app.core.config import Settings
    from app.models import CommandRun
    from app.services.commands import CommandRuntime, CommandService
    from tests.test_commands import FakeSession

    run = CommandRun(id=uuid4(), workspace_id=uuid4(), user_id=uuid4(), status="running", output="")
    runtime = CommandRuntime()
    queue = runtime.subscribe(run.id)
    service = CommandService(
        session=FakeSession(),  # type: ignore[arg-type]
        session_maker=None,  # type: ignore[arg-type]
        settings=Settings(workspace_root=tmp_path, agent_max_command_output_chars=5),
        runtime=runtime,
    )

    for chunk in ("abc", "def", "ghi"):
        await service._append_output(FakeSession(), run, chunk)  # type: ignore[arg-type]

    frames = [queue.get_nowait() for _ in range(queue.qsize())]
    # Live frames carry exactly what was persisted, so a reconnect sees the same output.
    assert frames == [
        {"type": "output", "content": "abc", "offset": 0},
        {"type": "output", "content": "de", "offset": 3},
    ]
    assert run.output == "abcde"


# ---------------------------------------------------------------------------
# Acceptance: replicas and a worker on separate Redis buses (one fakeredis server)
# ---------------------------------------------------------------------------
# "Stop on replica B halts a run on the worker" is covered by
# tests/test_redis_event_bus.py::test_stop_published_by_the_api_pauses_the_worker_provider.


def make_bus(server):
    from app.services.runtime import RedisEventBus

    return RedisEventBus(fakeredis.aioredis.FakeRedis(server=server, decode_responses=True))


async def wait_for_output(frames: list[dict], expected: str) -> None:
    async def until() -> None:
        while output_of(frames) != expected:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(until(), 2.0)


async def test_command_output_reaches_any_replica_and_survives_a_replica_hop(db_session):
    from app.api.commands import tail_command_run
    from app.services.commands import CommandRuntime, CommandService

    server = fakeredis.FakeServer()
    worker_bus, replica_b, replica_c = make_bus(server), make_bus(server), make_bus(server)
    session, session_maker = db_session
    run = await seed_command_run(session, output="boot\n")
    worker = CommandService(
        session=session,
        session_maker=session_maker,
        settings=queue_settings(),
        runtime=CommandRuntime(bus=worker_bus),
    )

    def stream_on(bus, frames: list[dict]) -> asyncio.Task:
        async def send(frame: dict) -> None:
            frames.append(frame)

        return asyncio.create_task(
            tail_command_run(
                send,
                CommandRuntime(bus=bus),
                session_maker,
                queue_settings(),
                run.workspace_id,
                run.id,
                run.user_id,
            )
        )

    try:
        on_b: list[dict] = []
        stream_b = stream_on(replica_b, on_b)
        await wait_for_output(on_b, "boot\n")  # backfill, then live
        await worker._append_output(session, run, "one\n")
        await wait_for_output(on_b, "boot\none\n")

        stream_b.cancel()  # replica B dies; the client reconnects to replica C
        await asyncio.gather(stream_b, return_exceptions=True)
        await worker._append_output(session, run, "two\n")  # nobody is listening

        on_c: list[dict] = []
        stream_c = stream_on(replica_c, on_c)
        await wait_for_output(on_c, "boot\none\ntwo\n")
        await worker._append_output(session, run, "three\n")
        await wait_for_output(on_c, "boot\none\ntwo\nthree\n")
        await worker._mark_finished(session, run, "completed", 0)
        await asyncio.wait_for(stream_c, 2.0)

        assert on_c[-1] == {"type": "status", "status": "completed", "exit_code": 0}
        assert output_of(on_c) == "boot\none\ntwo\nthree\n"  # in order, no gaps, no duplicates
    finally:
        for bus in (worker_bus, replica_b, replica_c):
            await bus.aclose()


async def test_a_notification_reaches_every_tab_on_every_replica():
    from app.services.notifications import NotificationRuntime

    server = fakeredis.FakeServer()
    worker_bus, replica_a, replica_b = make_bus(server), make_bus(server), make_bus(server)
    user_id = uuid4()
    tabs = [NotificationRuntime(replica_a), NotificationRuntime(replica_b)]
    try:
        queues = [tab.subscribe(user_id) for tab in tabs]
        for tab in tabs:
            assert await tab.bus.wait_ready(f"notifications:{user_id}")
        other_user = NotificationRuntime(replica_a).subscribe(uuid4())

        await NotificationRuntime(worker_bus).publish(user_id, {"id": "n1"})

        for queue in queues:
            assert await asyncio.wait_for(queue.get(), 2.0) == {"id": "n1"}
        assert other_user.empty()
    finally:
        for bus in (worker_bus, replica_a, replica_b):
            await bus.aclose()
