"""Epic 02/03 slice: the Redis pub/sub EventBus that carries run events and stop signals between
an API replica and the worker executing the run."""

import asyncio
import os
from uuid import uuid4

import fakeredis
import pytest
from redis.asyncio import Redis

from app.services.agents import AgentRuntime
from app.services.runtime import EventBus, InMemoryEventBus, InMemoryRunRegistry, RedisEventBus


class FakeProvider:
    def __init__(self) -> None:
        self.paused = False

    def pause(self) -> None:
        self.paused = True


@pytest.fixture
def server():
    return fakeredis.FakeServer()


def make_bus(server) -> RedisEventBus:
    return RedisEventBus(fakeredis.aioredis.FakeRedis(server=server, decode_responses=True))


async def next_payload(queue: asyncio.Queue, timeout: float = 2.0) -> dict:
    return await asyncio.wait_for(queue.get(), timeout)


def test_both_buses_satisfy_the_protocol(server):
    assert isinstance(InMemoryEventBus(), EventBus)
    assert isinstance(make_bus(server), EventBus)


async def test_publish_on_one_process_reaches_subscribers_on_another(server):
    api, worker = make_bus(server), make_bus(server)
    try:
        queue = api.subscribe("agent_run:1")
        assert await api.wait_ready("agent_run:1")

        payload = {"id": str(uuid4()), "event_type": "output_text", "content": "hi"}
        await worker.publish("agent_run:1", payload)

        assert await next_payload(queue) == payload
    finally:
        await api.aclose()
        await worker.aclose()


async def test_topics_are_isolated_and_every_local_subscriber_gets_a_copy(server):
    api, worker = make_bus(server), make_bus(server)
    try:
        first, second = api.subscribe("t:a"), api.subscribe("t:a")
        other = api.subscribe("t:b")
        assert await api.wait_ready("t:a") and await api.wait_ready("t:b")

        await worker.publish("t:a", {"n": 1})

        assert await next_payload(first) == {"n": 1}
        assert await next_payload(second) == {"n": 1}
        await asyncio.sleep(0.05)
        assert other.empty()
    finally:
        await api.aclose()
        await worker.aclose()


async def test_unsubscribe_stops_delivery_and_resubscribe_resumes(server):
    api, worker = make_bus(server), make_bus(server)
    try:
        queue = api.subscribe("t")
        assert await api.wait_ready("t")
        api.unsubscribe("t", queue)
        await worker.publish("t", {"n": 1})
        await asyncio.sleep(0.05)
        assert queue.empty()

        again = api.subscribe("t")
        assert await api.wait_ready("t")
        await worker.publish("t", {"n": 2})
        assert await next_payload(again) == {"n": 2}
    finally:
        await api.aclose()
        await worker.aclose()


async def test_non_json_native_values_are_stringified(server):
    api, worker = make_bus(server), make_bus(server)
    try:
        queue = api.subscribe("t")
        assert await api.wait_ready("t")
        run_id = uuid4()
        await worker.publish("t", {"run_id": run_id})
        assert await next_payload(queue) == {"run_id": str(run_id)}
    finally:
        await api.aclose()
        await worker.aclose()


async def test_in_memory_bus_is_always_ready():
    bus = InMemoryEventBus()
    bus.subscribe("t")
    assert await bus.wait_ready("t")


async def test_stop_published_by_the_api_pauses_the_worker_provider(server):
    """`stop` from any replica reaches the run on whichever worker executes it."""
    api_runtime = AgentRuntime(InMemoryRunRegistry(), make_bus(server))
    worker_runtime = AgentRuntime(InMemoryRunRegistry(), make_bus(server))
    run_id = uuid4()
    stop_event = asyncio.Event()
    provider = FakeProvider()
    holder = asyncio.create_task(asyncio.sleep(3600))
    worker_runtime.register_run(run_id, holder, stop_event)
    worker_runtime.register_provider(run_id, provider)
    try:
        async with worker_runtime.watch_control(run_id):
            await api_runtime.publish_control(run_id, "stop")
            await asyncio.wait_for(stop_event.wait(), 2.0)
        assert provider.paused
    finally:
        holder.cancel()
        await api_runtime.bus.aclose()
        await worker_runtime.bus.aclose()


@pytest.mark.skipif(
    os.environ.get("SKIP_REDIS_TESTS") == "1", reason="real Redis explicitly disabled"
)
async def test_round_trip_against_real_redis():
    from app.core.config import get_settings

    client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis is not reachable")
    await client.aclose()

    api = RedisEventBus(Redis.from_url(get_settings().redis_url, decode_responses=True))
    worker = RedisEventBus(Redis.from_url(get_settings().redis_url, decode_responses=True))
    topic = f"test:{uuid4()}"
    try:
        queue = api.subscribe(topic)
        assert await api.wait_ready(topic)
        await worker.publish(topic, {"ok": True})
        assert await next_payload(queue) == {"ok": True}
    finally:
        await api.aclose()
        await worker.aclose()


async def test_publish_is_best_effort_when_redis_is_down():
    """Publishing is at-most-once; a Redis blip must never fail the run that publishes."""

    class DownRedis:
        def pubsub(self):
            return None

        async def publish(self, *_args):
            raise ConnectionError("redis down")

    bus = RedisEventBus(DownRedis())  # type: ignore[arg-type]
    await bus.publish("agent_run:1", {"n": 1})  # must not raise


async def test_a_failed_subscribe_is_retried_while_someone_still_listens(server, monkeypatch):
    api, worker = make_bus(server), make_bus(server)
    real_subscribe = api._pubsub.subscribe
    failures = {"left": 1}

    async def flaky_subscribe(*channels):
        if failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("redis blip")
        return await real_subscribe(*channels)

    monkeypatch.setattr(api._pubsub, "subscribe", flaky_subscribe)
    monkeypatch.setattr(RedisEventBus, "SUBSCRIBE_RETRY_SECONDS", 0.05)
    try:
        queue = api.subscribe("t")
        assert await api.wait_ready("t")
        await worker.publish("t", {"n": 1})
        assert await next_payload(queue) == {"n": 1}
    finally:
        await api.aclose()
        await worker.aclose()
