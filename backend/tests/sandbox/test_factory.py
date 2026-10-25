from __future__ import annotations

import pytest

from app.services.sandbox import (
    RuntimeUnavailableError,
    SandboxRuntimeError,
    close_all,
    get_runtime,
    get_runtime_by_name,
    get_sandbox_runtime,
)
from app.services.sandbox import factory as factory_module
from app.services.sandbox.daytona import DaytonaRuntime
from tests.sandbox.fakes import FakeSandboxRuntime, make_workspace


@pytest.fixture(autouse=True)
async def _isolated_registry():
    await close_all()
    yield
    await close_all()
    factory_module._FACTORIES.pop("fake", None)


async def test_daytona_is_the_default_provider_and_a_singleton() -> None:
    workspace = make_workspace()
    runtime = get_runtime(workspace)
    assert isinstance(runtime, DaytonaRuntime)
    assert get_runtime(workspace) is runtime
    assert get_runtime_by_name("daytona") is runtime
    assert get_runtime(make_workspace(runtime=None)) is runtime


async def test_unknown_runtime_is_a_clear_unavailable_error_not_a_crash() -> None:
    with pytest.raises(RuntimeUnavailableError, match="Unknown sandbox runtime 'bogus'"):
        get_runtime(make_workspace(runtime="bogus"))

    # The dispatcher surfaces the same error, so the API routes' SandboxRuntimeError
    # handlers turn it into a 4xx/503 instead of a 500.
    with pytest.raises(SandboxRuntimeError):
        await get_sandbox_runtime().exec(make_workspace(runtime="bogus"), "true")


async def test_dispatcher_routes_each_call_to_the_workspace_provider() -> None:
    fake = FakeSandboxRuntime()
    factory_module._FACTORIES["fake"] = lambda settings: fake
    workspace = make_workspace("sandbox-x", runtime="fake")
    dispatcher = get_sandbox_runtime()

    await dispatcher.exec(workspace, "pwd", cwd="/workspace")
    assert fake.exec_calls == [
        {"command": "pwd", "cwd": "/workspace", "env": None, "timeout": None}
    ]
    assert await dispatcher.fs(workspace) is fake.filesystem
    assert await dispatcher.provision(workspace) == f"sandbox-{workspace.id}"
    assert (await dispatcher.preview_link(workspace, 80)).url == "http://sandbox-x:80"
    assert dispatcher.provider == "dispatch"


async def test_close_all_keeps_going_when_one_provider_fails_to_close() -> None:
    class Broken(FakeSandboxRuntime):
        async def close(self) -> None:
            raise RuntimeError("socket already gone")

    broken, fake = Broken(), FakeSandboxRuntime()
    factory_module._FACTORIES["broken"] = lambda settings: broken
    factory_module._FACTORIES["fake"] = lambda settings: fake
    get_runtime_by_name("broken")
    get_runtime_by_name("fake")

    await close_all()

    assert fake.closed is True
    assert factory_module._instances == {}
    factory_module._FACTORIES.pop("broken", None)


async def test_close_all_closes_every_provider_and_forgets_it() -> None:
    fake = FakeSandboxRuntime()
    factory_module._FACTORIES["fake"] = lambda settings: fake
    assert get_runtime_by_name("fake") is fake

    await get_sandbox_runtime().close()

    assert fake.closed is True
    assert factory_module._instances == {}
