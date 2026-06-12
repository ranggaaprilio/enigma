import asyncio
import json
import tempfile
import threading
from collections.abc import AsyncIterator
from typing import Any

from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.sdk.event import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    MessageEvent,
    ObservationEvent,
)
from openhands.sdk.llm.message import TextContent

from app.services.agent_providers import AgentProvider, ProviderEvent
from app.services.agent_tools import WorkspaceTools
from app.services.openhands_tools import (
    DaytonaToolBridge,
    register_daytona_bridge,
    unregister_daytona_bridge,
)

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
}


class OpenHandsConfigurationError(ValueError):
    pass


def normalize_model_name(model: str | None) -> str:
    model_name = (model or "").strip()
    if model_name.startswith("opencode-go/"):
        model_name = model_name.removeprefix("opencode-go/")
    if model_name and "/" not in model_name:
        return f"openai/{model_name}"
    return model_name


def sanitize_metadata(value: Any, key: str | None = None) -> Any:
    if key and any(sensitive in key.lower() for sensitive in SENSITIVE_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_metadata(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_metadata(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class OpenHandsAgentProvider(AgentProvider):
    provider_name = "openhands"

    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        model: str | None,
        max_iterations: int,
    ) -> None:
        self.base_url = (base_url or "").strip()
        self.api_key = (api_key or "").strip() or None
        self.model_name = normalize_model_name(model)
        self.max_iterations = max_iterations
        self._conversation: Any | None = None
        self._bridge: DaytonaToolBridge | None = None
        self._lifecycle_lock = threading.Lock()
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        if not self.base_url:
            raise OpenHandsConfigurationError("OPENHANDS_LLM_BASE_URL is required")
        if not self.model_name:
            raise OpenHandsConfigurationError("OPENHANDS_LLM_MODEL is required")
        if self.max_iterations < 1:
            raise OpenHandsConfigurationError("OPENHANDS_MAX_ITERATIONS must be at least 1")

    def pause(self) -> None:
        with self._lifecycle_lock:
            conversation = self._conversation
            bridge = self._bridge
        if bridge is not None:
            bridge.interrupt()
        if conversation is not None:
            try:
                conversation.pause()
            except Exception:
                pass

    def _create_llm(self) -> LLM:
        extra_headers = None if self.api_key else {"Authorization": ""}
        return LLM(
            usage_id="agent",
            model=self.model_name,
            base_url=self.base_url,
            # An explicit placeholder prevents LiteLLM from falling back to
            # an unrelated OPENAI_API_KEY loaded from the process environment.
            api_key=self.api_key or "enigma-no-key",
            extra_headers=extra_headers,
            stream=True,
        )

    async def stream_response(
        self,
        prompt: str,
        tools: WorkspaceTools,
        stop_event: asyncio.Event,
    ) -> AsyncIterator[ProviderEvent]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[ProviderEvent | Exception | None] = asyncio.Queue()

        def enqueue(item: ProviderEvent | Exception | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def worker() -> None:
            bridge = DaytonaToolBridge(tools, loop, stop_event)
            bridge_id = register_daytona_bridge(bridge)
            conversation = None
            streamed_text = False

            def on_token(chunk: Any) -> None:
                nonlocal streamed_text
                text = self._token_text(chunk)
                if text and not stop_event.is_set():
                    streamed_text = True
                    enqueue(
                        ProviderEvent(
                            "output_text",
                            text,
                            {"openhands_event_type": type(chunk).__name__},
                        )
                    )

            def on_event(event: Event) -> None:
                mapped = self.map_event(event, include_agent_message=not streamed_text)
                if mapped is not None and not stop_event.is_set():
                    enqueue(mapped)

            try:
                llm = self._create_llm()
                agent = Agent(
                    llm=llm,
                    tools=[
                        Tool(name="list_files", params={"bridge_id": bridge_id}),
                        Tool(name="read_file", params={"bridge_id": bridge_id}),
                        Tool(name="write_file", params={"bridge_id": bridge_id}),
                        Tool(name="run_command", params={"bridge_id": bridge_id}),
                    ],
                )
                with tempfile.TemporaryDirectory(prefix="enigma-openhands-") as workspace:
                    conversation = Conversation(
                        agent=agent,
                        workspace=workspace,
                        callbacks=[on_event],
                        token_callbacks=[on_token],
                        max_iteration_per_run=self.max_iterations,
                        stuck_detection=True,
                        visualizer=None,
                    )
                    with self._lifecycle_lock:
                        self._conversation = conversation
                        self._bridge = bridge
                    if stop_event.is_set():
                        conversation.pause()
                        return
                    conversation.send_message(prompt)
                    conversation.run()
                    if stop_event.is_set():
                        return
                    status = str(conversation.state.execution_status.value)
                    if status != "finished":
                        raise RuntimeError(
                            f"OpenHands conversation finished with status {status}"
                        )
                    usage = llm.metrics.accumulated_token_usage
                    tokens_used = usage.prompt_tokens + usage.completion_tokens
                    enqueue(
                        ProviderEvent(
                            "status",
                            "completed",
                            {
                                "status": "completed",
                                "tokens_used": tokens_used,
                                "openhands_status": status,
                            },
                        )
                    )
            except Exception as error:
                enqueue(error)
            finally:
                if conversation is not None:
                    try:
                        conversation.close()
                    except Exception:
                        pass
                with self._lifecycle_lock:
                    self._conversation = None
                    self._bridge = None
                unregister_daytona_bridge(bridge_id)
                enqueue(None)

        yield ProviderEvent("status", "thinking", {"status": "thinking"})
        if stop_event.is_set():
            return
        yield ProviderEvent("status", "executing", {"status": "executing"})
        task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            if stop_event.is_set():
                self.pause()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                self.pause()
                raise

    @staticmethod
    def _token_text(chunk: Any) -> str:
        parts: list[str] = []
        for choice in getattr(chunk, "choices", []) or []:
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None)
            if isinstance(content, str):
                parts.append(content)
        return "".join(parts)

    @staticmethod
    def _message_text(event: MessageEvent) -> str:
        return "".join(
            content.text
            for content in event.llm_message.content
            if isinstance(content, TextContent)
        )

    @classmethod
    def map_event(
        cls,
        event: Event,
        *,
        include_agent_message: bool = True,
    ) -> ProviderEvent | None:
        event_name = type(event).__name__
        if isinstance(event, ActionEvent):
            try:
                arguments = json.loads(event.tool_call.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"raw": event.tool_call.arguments}
            return ProviderEvent(
                "tool_call",
                event.summary or event.tool_name,
                {
                    "openhands_event_type": event_name,
                    "tool": event.tool_name,
                    "tool_call_id": str(event.tool_call_id),
                    "arguments": sanitize_metadata(arguments),
                },
            )
        if isinstance(event, ObservationEvent):
            observation = event.observation
            metadata = {
                "openhands_event_type": event_name,
                "tool": event.tool_name,
                "tool_call_id": str(event.tool_call_id),
                "ok": not observation.is_error,
            }
            for field_name in ("path", "exit_code", "truncated"):
                value = getattr(observation, field_name, None)
                if value is not None:
                    metadata[field_name] = value
            return ProviderEvent(
                "tool_result",
                observation.text,
                sanitize_metadata(metadata),
            )
        if isinstance(event, AgentErrorEvent):
            return ProviderEvent(
                "tool_result",
                event.error,
                {
                    "openhands_event_type": event_name,
                    "tool": event.tool_name,
                    "tool_call_id": str(event.tool_call_id),
                    "ok": False,
                },
            )
        if (
            include_agent_message
            and isinstance(event, MessageEvent)
            and event.source == "agent"
        ):
            text = cls._message_text(event)
            if text:
                return ProviderEvent(
                    "output_text",
                    text,
                    {"openhands_event_type": event_name, "final_message": True},
                )
        return None
