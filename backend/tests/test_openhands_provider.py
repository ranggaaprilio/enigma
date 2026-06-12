import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest
from openhands.sdk.event import ActionEvent, MessageEvent, ObservationEvent
from openhands.sdk.llm.message import Message, MessageToolCall, TextContent

from app.core.config import Settings
from app.services.agent_tools import CommandResult
from app.services.agents import (
    AgentConfigurationError,
    AgentRuntime,
    provider_from_settings,
)
from app.services.openhands_provider import (
    OpenHandsAgentProvider,
    sanitize_metadata,
)
from app.services.openhands_tools import (
    DaytonaToolBridge,
    DaytonaToolObservation,
    ListFilesAction,
    ListFilesTool,
    RunCommandAction,
    RunCommandTool,
    register_daytona_bridge,
    unregister_daytona_bridge,
)


class FakeWorkspaceTools:
    def __init__(self) -> None:
        self.list_calls: list[tuple[str | None, int]] = []
        self.command_calls: list[str] = []

    async def list_files(self, path: str | None = None, limit: int = 200) -> list[str]:
        self.list_calls.append((path, limit))
        return ["README.md", "src/app.py"]

    async def run_command(self, command: str) -> CommandResult:
        self.command_calls.append(command)
        return CommandResult(exit_code=0, stdout="ok", stderr="", truncated=False)


def test_openhands_provider_requires_base_url_and_model() -> None:
    with pytest.raises(AgentConfigurationError, match="OPENHANDS_LLM_BASE_URL"):
        provider_from_settings(
            Settings(
                agent_provider="openhands",
                openhands_llm_base_url=None,
                openhands_llm_model="openai/test",
            )
        )

    with pytest.raises(AgentConfigurationError, match="OPENHANDS_LLM_MODEL"):
        provider_from_settings(
            Settings(
                agent_provider="openhands",
                openhands_llm_base_url="http://llm.example/v1",
                openhands_llm_model=None,
            )
        )


def test_openhands_provider_accepts_optional_api_key() -> None:
    provider = provider_from_settings(
        Settings(
            agent_provider="openhands",
            openhands_llm_base_url="http://llm.example/v1",
            openhands_llm_model="openai/local-model",
            openhands_llm_api_key="",
        )
    )

    assert isinstance(provider, OpenHandsAgentProvider)
    assert provider.api_key is None
    assert provider.model_name == "openai/local-model"


@pytest.mark.parametrize(
    ("configured_model", "litellm_model"),
    [
        ("deepseek-v4-pro", "openai/deepseek-v4-pro"),
        ("opencode-go/deepseek-v4-pro", "openai/deepseek-v4-pro"),
        ("openai/deepseek-v4-pro", "openai/deepseek-v4-pro"),
    ],
)
def test_openhands_provider_normalizes_openai_compatible_model_names(
    configured_model: str,
    litellm_model: str,
) -> None:
    provider = OpenHandsAgentProvider(
        base_url="https://opencode.ai/zen/go/v1",
        api_key="test-key",
        model=configured_model,
        max_iterations=3,
    )

    assert provider.model_name == litellm_model


def test_openhands_llm_calls_openai_compatible_chat_completions() -> None:
    requests: list[tuple[str, str | None, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length))
            requests.append((self.path, self.headers.get("authorization"), payload))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "gpt-4o-mini",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "Hello"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "gpt-4o-mini",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                },
            ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, format: str, *args) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = OpenHandsAgentProvider(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key=None,
            model="openai/gpt-4o-mini",
            max_iterations=3,
        )
        chunks = []
        response = provider._create_llm().completion(
            [
                Message(
                    role="user",
                    content=[TextContent(text="Say hello")],
                )
            ],
            on_token=chunks.append,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert response.message.content[0].text == "Hello"
    assert requests[0][0] == "/v1/chat/completions"
    assert requests[0][1] == ""
    assert requests[0][2]["model"] == "gpt-4o-mini"
    assert requests[0][2]["stream"] is True


def test_metadata_sanitizer_redacts_nested_secrets() -> None:
    sanitized = sanitize_metadata(
        {
            "arguments": {
                "path": "README.md",
                "authorization": "Bearer secret",
                "nested": {"api_key": "top-secret"},
            }
        }
    )

    assert sanitized["arguments"]["path"] == "README.md"
    assert sanitized["arguments"]["authorization"] == "[redacted]"
    assert sanitized["arguments"]["nested"]["api_key"] == "[redacted]"


def test_openhands_event_mapping() -> None:
    action = ListFilesAction(path="src", limit=10)
    tool_call = MessageToolCall(
        id="call-1",
        name="list_files",
        arguments='{"path":"src","authorization":"secret"}',
        origin="completion",
    )
    action_event = ActionEvent(
        thought=[],
        action=action,
        tool_name="list_files",
        tool_call_id="call-1",
        tool_call=tool_call,
        llm_response_id="response-1",
        summary="Inspect source files",
    )
    observation_event = ObservationEvent(
        tool_name="list_files",
        tool_call_id="call-1",
        observation=DaytonaToolObservation.from_text(text="src/app.py"),
        action_id=action_event.id,
    )
    message_event = MessageEvent(
        source="agent",
        llm_message=Message(
            role="assistant",
            content=[TextContent(text="Done")],
        ),
    )

    mapped_action = OpenHandsAgentProvider.map_event(action_event)
    mapped_observation = OpenHandsAgentProvider.map_event(observation_event)
    mapped_message = OpenHandsAgentProvider.map_event(message_event)

    assert mapped_action is not None
    assert mapped_action.event_type == "tool_call"
    assert mapped_action.metadata["tool_call_id"] == "call-1"
    assert mapped_action.metadata["arguments"]["authorization"] == "[redacted]"
    assert mapped_observation is not None
    assert mapped_observation.event_type == "tool_result"
    assert mapped_observation.metadata["ok"] is True
    assert mapped_message is not None
    assert mapped_message.content == "Done"
    assert OpenHandsAgentProvider.map_event(
        message_event,
        include_agent_message=False,
    ) is None


async def test_daytona_tools_delegate_to_workspace_tools() -> None:
    tools = FakeWorkspaceTools()
    stop_event = asyncio.Event()
    bridge_id = register_daytona_bridge(
        DaytonaToolBridge(tools, asyncio.get_running_loop(), stop_event)  # type: ignore[arg-type]
    )
    try:
        list_tool = ListFilesTool.create(None, bridge_id)[0]
        command_tool = RunCommandTool.create(None, bridge_id)[0]

        listed = await asyncio.to_thread(list_tool, ListFilesAction(path="src", limit=5))
        command = await asyncio.to_thread(
            command_tool,
            RunCommandAction(command="pytest"),
        )

        assert listed.text == "README.md\nsrc/app.py"
        assert tools.list_calls == [("src", 5)]
        assert command.exit_code == 0
        assert command.stdout == "ok"
        assert tools.command_calls == ["pytest"]
    finally:
        unregister_daytona_bridge(bridge_id)


async def test_daytona_tools_reject_operations_after_stop() -> None:
    tools = FakeWorkspaceTools()
    stop_event = asyncio.Event()
    stop_event.set()
    bridge_id = register_daytona_bridge(
        DaytonaToolBridge(tools, asyncio.get_running_loop(), stop_event)  # type: ignore[arg-type]
    )
    try:
        tool = ListFilesTool.create(None, bridge_id)[0]
        result = await asyncio.to_thread(tool, ListFilesAction())

        assert result.is_error is True
        assert "stopped" in result.text
        assert tools.list_calls == []
    finally:
        unregister_daytona_bridge(bridge_id)


async def test_openhands_stream_maps_callbacks_and_usage(monkeypatch) -> None:
    import app.services.openhands_provider as provider_module

    captured_llm: dict = {}

    class FakeLLM:
        def __init__(self, **kwargs) -> None:
            captured_llm.update(kwargs)
            usage = SimpleNamespace(prompt_tokens=8, completion_tokens=5)
            self.metrics = SimpleNamespace(accumulated_token_usage=usage)

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class FakeConversation:
        def __init__(self, **kwargs) -> None:
            self.callbacks = kwargs["callbacks"]
            self.token_callbacks = kwargs["token_callbacks"]
            self.state = SimpleNamespace(
                execution_status=SimpleNamespace(value="finished")
            )
            self.paused = False
            self.closed = False

        def send_message(self, prompt: str) -> None:
            self.prompt = prompt

        def run(self) -> None:
            chunk = SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello"))]
            )
            self.token_callbacks[0](chunk)
            event = MessageEvent(
                source="agent",
                llm_message=Message(
                    role="assistant",
                    content=[TextContent(text="Hello")],
                ),
            )
            self.callbacks[0](event)

        def pause(self) -> None:
            self.paused = True

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(provider_module, "LLM", FakeLLM)
    monkeypatch.setattr(provider_module, "Agent", FakeAgent)
    monkeypatch.setattr(provider_module, "Conversation", FakeConversation)

    provider = OpenHandsAgentProvider(
        base_url="http://llm.example/v1",
        api_key=None,
        model="openai/local-model",
        max_iterations=7,
    )
    events = [
        event
        async for event in provider.stream_response(
            "Inspect the repository",
            FakeWorkspaceTools(),  # type: ignore[arg-type]
            asyncio.Event(),
        )
    ]

    assert [event.event_type for event in events] == [
        "status",
        "status",
        "output_text",
        "status",
    ]
    assert [event.content for event in events if event.event_type == "output_text"] == [
        "Hello"
    ]
    assert events[-1].metadata["tokens_used"] == 13
    assert captured_llm["base_url"] == "http://llm.example/v1"
    assert captured_llm["model"] == "openai/local-model"
    assert captured_llm["api_key"] == "enigma-no-key"
    assert captured_llm["stream"] is True


def test_agent_runtime_stop_is_cooperative() -> None:
    class FakeProvider:
        def __init__(self) -> None:
            self.paused = False

        def pause(self) -> None:
            self.paused = True

    runtime = AgentRuntime()
    run_id = uuid4()
    stop_event = asyncio.Event()
    provider = FakeProvider()
    runtime.stop_events[run_id] = stop_event
    runtime.providers[run_id] = provider  # type: ignore[assignment]

    runtime.request_stop(run_id)

    assert stop_event.is_set()
    assert provider.paused is True
