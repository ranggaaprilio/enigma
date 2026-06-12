import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import uuid4

from app.services.agent_tools import WorkspaceTools


@dataclass(frozen=True)
class ProviderEvent:
    event_type: str
    content: str
    metadata: dict = field(default_factory=dict)


class AgentProvider:
    provider_name = "mock"
    model_name = "mock-agent"

    async def stream_response(
        self,
        prompt: str,
        tools: WorkspaceTools,
        stop_event: asyncio.Event,
    ) -> AsyncIterator[ProviderEvent]:
        raise NotImplementedError

    def pause(self) -> None:
        return None


class MockAgentProvider(AgentProvider):
    provider_name = "mock"
    model_name = "mock-agent"

    async def stream_response(
        self,
        prompt: str,
        tools: WorkspaceTools,
        stop_event: asyncio.Event,
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent("status", "thinking", {"status": "thinking"})
        for chunk in ("I received your prompt. ", "Inspecting the workspace. "):
            if stop_event.is_set():
                return
            await asyncio.sleep(0.01)
            yield ProviderEvent("output_text", chunk)

        if stop_event.is_set():
            return
        yield ProviderEvent("status", "executing", {"status": "executing"})
        tool_call_id = f"mock-tool-{uuid4().hex[:8]}"
        yield ProviderEvent(
            "tool_call",
            "list_files",
            {"tool": "list_files", "arguments": {}, "tool_call_id": tool_call_id},
        )
        try:
            files = await tools.list_files(limit=20)
            yield ProviderEvent(
                "tool_result",
                "\n".join(files) if files else "Workspace has no visible files.",
                {"tool": "list_files", "ok": True, "tool_call_id": tool_call_id},
            )
        except Exception as error:
            yield ProviderEvent(
                "tool_result",
                str(error),
                {"tool": "list_files", "ok": False, "tool_call_id": tool_call_id},
            )

        if stop_event.is_set():
            return
        await asyncio.sleep(0.01)
        yield ProviderEvent(
            "output_text",
            f"Mock run complete for: {prompt[:160]}",
            {"tokens_used": max(1, len(prompt.split()))},
        )


class OpenAIResponsesProvider(AgentProvider):
    provider_name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model_name = model

    async def stream_response(
        self,
        prompt: str,
        tools: WorkspaceTools,
        stop_event: asyncio.Event,
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent("status", "thinking", {"status": "thinking"})
        async for event in self._stream_openai(prompt, stop_event):
            yield event

    async def _stream_openai(
        self,
        prompt: str,
        stop_event: asyncio.Event,
    ) -> AsyncIterator[ProviderEvent]:
        queue: asyncio.Queue[ProviderEvent | Exception | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def worker() -> None:
            try:
                payload = {
                    "model": self.model_name,
                    "input": prompt,
                    "stream": True,
                }
                request = urllib.request.Request(
                    "https://api.openai.com/v1/responses",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=60) as response:
                    for raw_line in response:
                        if stop_event.is_set():
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            payload = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        parsed = self._parse_stream_payload(payload)
                        if parsed is not None:
                            loop.call_soon_threadsafe(queue.put_nowait, parsed)
            except Exception as error:
                loop.call_soon_threadsafe(queue.put_nowait, error)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            while True:
                next_item = await queue.get()
                if next_item is None:
                    break
                if isinstance(next_item, Exception):
                    raise next_item
                yield next_item
        finally:
            task.cancel()

    def _parse_stream_payload(self, payload: dict) -> ProviderEvent | None:
        event_type = payload.get("type")
        if event_type == "response.output_text.delta":
            return ProviderEvent("output_text", str(payload.get("delta", "")))
        if event_type == "response.completed":
            response = payload.get("response") or {}
            usage = response.get("usage") or {}
            total_tokens = usage.get("total_tokens")
            return ProviderEvent(
                "status",
                "completed",
                {"status": "completed", "tokens_used": total_tokens},
            )
        if event_type == "response.failed":
            error = (payload.get("response") or {}).get("error") or {}
            return ProviderEvent("error", str(error.get("message") or "OpenAI response failed"))
        return None
