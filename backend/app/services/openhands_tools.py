import asyncio
import threading
from collections.abc import Coroutine, Sequence
from concurrent.futures import Future
from typing import Any
from uuid import uuid4

from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)
from pydantic import Field

from app.services.agent_tools import WorkspaceTools


class OpenHandsToolStoppedError(Exception):
    pass


class DaytonaToolBridge:
    def __init__(
        self,
        tools: WorkspaceTools,
        loop: asyncio.AbstractEventLoop,
        stop_event: asyncio.Event,
    ) -> None:
        self.tools = tools
        self.loop = loop
        self.stop_event = stop_event
        self._futures: set[Future[Any]] = set()
        self._futures_lock = threading.Lock()

    def execute(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        if self.stop_event.is_set():
            coroutine.close()
            raise OpenHandsToolStoppedError("Agent run was stopped")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        with self._futures_lock:
            self._futures.add(future)
        try:
            result = future.result()
        except BaseException:
            future.cancel()
            raise
        finally:
            with self._futures_lock:
                self._futures.discard(future)
        if self.stop_event.is_set():
            raise OpenHandsToolStoppedError("Agent run was stopped")
        return result

    def interrupt(self) -> None:
        with self._futures_lock:
            futures = list(self._futures)
        for future in futures:
            future.cancel()


_BRIDGES: dict[str, DaytonaToolBridge] = {}
_BRIDGES_LOCK = threading.Lock()


def register_daytona_bridge(bridge: DaytonaToolBridge) -> str:
    bridge_id = uuid4().hex
    with _BRIDGES_LOCK:
        _BRIDGES[bridge_id] = bridge
    return bridge_id


def unregister_daytona_bridge(bridge_id: str) -> None:
    with _BRIDGES_LOCK:
        _BRIDGES.pop(bridge_id, None)


def get_daytona_bridge(bridge_id: str) -> DaytonaToolBridge:
    with _BRIDGES_LOCK:
        bridge = _BRIDGES.get(bridge_id)
    if bridge is None:
        raise RuntimeError("Daytona tool bridge is no longer available")
    return bridge


class ListFilesAction(Action):
    path: str | None = Field(default=None, description="Relative directory to inspect.")
    limit: int = Field(default=200, ge=1, le=200)


class ReadFileAction(Action):
    path: str = Field(description="Relative path of the text file to read.")
    max_chars: int = Field(default=20_000, ge=1, le=100_000)


class WriteFileAction(Action):
    path: str = Field(description="Relative path of the file to create or replace.")
    content: str = Field(description="Complete UTF-8 content for the file.")


class RunCommandAction(Action):
    command: str = Field(
        description="A single safe command without shell control operators.",
        min_length=1,
        max_length=10_000,
    )


class ApplyPatchAction(Action):
    path: str = Field(description="Relative path of the file to edit.")
    patch: str = Field(
        description=(
            "One or more SEARCH/REPLACE blocks to apply to the file. Each block "
            "has the exact form:\n"
            "<<<<<<< SEARCH\n"
            "<exact existing text to find>\n"
            "=======\n"
            "<replacement text>\n"
            ">>>>>>> REPLACE\n"
            "The SEARCH text must match the current file content exactly and "
            "uniquely; otherwise the edit is rejected with no changes made."
        ),
        min_length=1,
        max_length=200_000,
    )


class SearchCodeAction(Action):
    query: str = Field(
        description="Regular-expression pattern to search for (ripgrep syntax).",
        min_length=1,
        max_length=1000,
    )
    path_glob: str | None = Field(
        default=None,
        description="Optional glob to scope the search, e.g. '*.py'.",
    )
    max_results: int = Field(default=200, ge=1, le=1000)


class GitStatusAction(Action):
    pass


class GitDiffAction(Action):
    path: str | None = Field(
        default=None,
        description="Optional relative path to diff a single file.",
    )


class DaytonaToolObservation(Observation):
    path: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    truncated: bool = False
    diff: str | None = None
    matches: list[dict[str, Any]] | None = None
    branch: str | None = None
    is_clean: bool | None = None
    files: list[dict[str, str]] | None = None


class DaytonaToolExecutor(ToolExecutor[Action, DaytonaToolObservation]):
    def __init__(self, bridge_id: str, operation: str) -> None:
        self.bridge_id = bridge_id
        self.operation = operation

    def __call__(
        self,
        action: Action,
        conversation: Any | None = None,
    ) -> DaytonaToolObservation:
        del conversation
        bridge = get_daytona_bridge(self.bridge_id)
        try:
            if self.operation == "list_files":
                assert isinstance(action, ListFilesAction)
                files = bridge.execute(bridge.tools.list_files(action.path, action.limit))
                content = "\n".join(files) if files else "Workspace has no visible files."
                return DaytonaToolObservation.from_text(text=content)
            if self.operation == "read_file":
                assert isinstance(action, ReadFileAction)
                content = bridge.execute(bridge.tools.read_file(action.path, action.max_chars))
                return DaytonaToolObservation.from_text(
                    text=content,
                    path=action.path,
                )
            if self.operation == "write_file":
                assert isinstance(action, WriteFileAction)
                path = bridge.execute(bridge.tools.write_file(action.path, action.content))
                return DaytonaToolObservation.from_text(
                    text=f"Wrote file: {path}",
                    path=path,
                )
            if self.operation == "run_command":
                assert isinstance(action, RunCommandAction)
                result = bridge.execute(bridge.tools.run_command(action.command))
                sections = []
                if result.stdout:
                    sections.append(result.stdout)
                if result.stderr:
                    sections.append(f"stderr:\n{result.stderr}")
                sections.append(f"exit_code: {result.exit_code}")
                if result.truncated:
                    sections.append("output_truncated: true")
                return DaytonaToolObservation.from_text(
                    text="\n".join(sections),
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    truncated=result.truncated,
                    is_error=result.exit_code != 0,
                )
            if self.operation == "apply_patch":
                assert isinstance(action, ApplyPatchAction)
                result = bridge.execute(bridge.tools.apply_patch(action.path, action.patch))
                return DaytonaToolObservation.from_text(
                    text=(
                        f"Applied {result.blocks_applied} block(s) to {result.path}\n\n"
                        f"{result.diff}"
                    ),
                    path=result.path,
                    diff=result.diff,
                )
            if self.operation == "search_code":
                assert isinstance(action, SearchCodeAction)
                result = bridge.execute(
                    bridge.tools.search_code(
                        action.query, action.path_glob, action.max_results
                    )
                )
                matches = [
                    {"path": m.path, "line_number": m.line_number, "text": m.text}
                    for m in result.matches
                ]
                text = (
                    "\n".join(f"{m['path']}:{m['line_number']}: {m['text']}" for m in matches)
                    or "No matches found."
                )
                return DaytonaToolObservation.from_text(
                    text=text,
                    matches=matches,
                    truncated=result.truncated,
                )
            if self.operation == "git_status":
                assert isinstance(action, GitStatusAction)
                result = bridge.execute(bridge.tools.git_status())
                files = [{"path": f.path, "status": f.status} for f in result.files]
                if result.is_clean:
                    body = "working tree clean"
                else:
                    body = "\n".join(f"{f['status']} {f['path']}" for f in files)
                text = f"On branch {result.branch or '(detached)'}\n{body}"
                return DaytonaToolObservation.from_text(
                    text=text,
                    branch=result.branch,
                    is_clean=result.is_clean,
                    files=files,
                )
            if self.operation == "git_diff":
                assert isinstance(action, GitDiffAction)
                diff = bridge.execute(bridge.tools.git_diff(action.path))
                return DaytonaToolObservation.from_text(
                    text=diff or "No changes.",
                    path=action.path,
                    diff=diff,
                )
            raise RuntimeError(f"Unsupported Daytona tool operation: {self.operation}")
        except Exception as error:
            return DaytonaToolObservation.from_text(text=str(error), is_error=True)

    def interrupt(self) -> None:
        get_daytona_bridge(self.bridge_id).interrupt()


class ListFilesTool(ToolDefinition[ListFilesAction, DaytonaToolObservation]):
    name = "list_files"

    @classmethod
    def create(
        cls,
        conv_state: Any,
        bridge_id: str,
    ) -> Sequence["ListFilesTool"]:
        del conv_state
        return [
            cls(
                description="List files in the Daytona workspace using relative paths.",
                action_type=ListFilesAction,
                observation_type=DaytonaToolObservation,
                annotations=ToolAnnotations(
                    title="list_files",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=DaytonaToolExecutor(bridge_id, "list_files"),
            )
        ]


class ReadFileTool(ToolDefinition[ReadFileAction, DaytonaToolObservation]):
    name = "read_file"

    @classmethod
    def create(
        cls,
        conv_state: Any,
        bridge_id: str,
    ) -> Sequence["ReadFileTool"]:
        del conv_state
        return [
            cls(
                description="Read a UTF-8 text file from the Daytona workspace.",
                action_type=ReadFileAction,
                observation_type=DaytonaToolObservation,
                annotations=ToolAnnotations(
                    title="read_file",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=DaytonaToolExecutor(bridge_id, "read_file"),
            )
        ]


class WriteFileTool(ToolDefinition[WriteFileAction, DaytonaToolObservation]):
    name = "write_file"

    @classmethod
    def create(
        cls,
        conv_state: Any,
        bridge_id: str,
    ) -> Sequence["WriteFileTool"]:
        del conv_state
        return [
            cls(
                description=(
                    "Create or replace a UTF-8 file in the Daytona workspace. "
                    "Paths must be relative to the workspace."
                ),
                action_type=WriteFileAction,
                observation_type=DaytonaToolObservation,
                annotations=ToolAnnotations(
                    title="write_file",
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=DaytonaToolExecutor(bridge_id, "write_file"),
            )
        ]


class RunCommandTool(ToolDefinition[RunCommandAction, DaytonaToolObservation]):
    name = "run_command"

    @classmethod
    def create(
        cls,
        conv_state: Any,
        bridge_id: str,
    ) -> Sequence["RunCommandTool"]:
        del conv_state
        return [
            cls(
                description=(
                    "Run one command in the Daytona workspace. Shell operators and "
                    "commands blocked by Enigma policy are rejected."
                ),
                action_type=RunCommandAction,
                observation_type=DaytonaToolObservation,
                annotations=ToolAnnotations(
                    title="run_command",
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
                executor=DaytonaToolExecutor(bridge_id, "run_command"),
            )
        ]


class ApplyPatchTool(ToolDefinition[ApplyPatchAction, DaytonaToolObservation]):
    name = "apply_patch"

    @classmethod
    def create(
        cls,
        conv_state: Any,
        bridge_id: str,
    ) -> Sequence["ApplyPatchTool"]:
        del conv_state
        return [
            cls(
                description=(
                    "Edit a file in place by applying SEARCH/REPLACE blocks. "
                    "Prefer this over write_file for targeted edits: it avoids "
                    "rewriting the whole file and fails loudly (no overwrite) "
                    "when the search context does not match exactly."
                ),
                action_type=ApplyPatchAction,
                observation_type=DaytonaToolObservation,
                annotations=ToolAnnotations(
                    title="apply_patch",
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
                executor=DaytonaToolExecutor(bridge_id, "apply_patch"),
            )
        ]


class SearchCodeTool(ToolDefinition[SearchCodeAction, DaytonaToolObservation]):
    name = "search_code"

    @classmethod
    def create(
        cls,
        conv_state: Any,
        bridge_id: str,
    ) -> Sequence["SearchCodeTool"]:
        del conv_state
        return [
            cls(
                description=(
                    "Search file contents in the workspace with a ripgrep-style "
                    "regex. Use this to locate symbols or text before editing."
                ),
                action_type=SearchCodeAction,
                observation_type=DaytonaToolObservation,
                annotations=ToolAnnotations(
                    title="search_code",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=DaytonaToolExecutor(bridge_id, "search_code"),
            )
        ]


class GitStatusTool(ToolDefinition[GitStatusAction, DaytonaToolObservation]):
    name = "git_status"

    @classmethod
    def create(
        cls,
        conv_state: Any,
        bridge_id: str,
    ) -> Sequence["GitStatusTool"]:
        del conv_state
        return [
            cls(
                description="Show the git status (branch and changed files) of the workspace.",
                action_type=GitStatusAction,
                observation_type=DaytonaToolObservation,
                annotations=ToolAnnotations(
                    title="git_status",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=DaytonaToolExecutor(bridge_id, "git_status"),
            )
        ]


class GitDiffTool(ToolDefinition[GitDiffAction, DaytonaToolObservation]):
    name = "git_diff"

    @classmethod
    def create(
        cls,
        conv_state: Any,
        bridge_id: str,
    ) -> Sequence["GitDiffTool"]:
        del conv_state
        return [
            cls(
                description=(
                    "Show the git diff of unstaged changes in the workspace, "
                    "optionally scoped to a single relative path."
                ),
                action_type=GitDiffAction,
                observation_type=DaytonaToolObservation,
                annotations=ToolAnnotations(
                    title="git_diff",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=DaytonaToolExecutor(bridge_id, "git_diff"),
            )
        ]


register_tool(ListFilesTool.name, ListFilesTool)
register_tool(ReadFileTool.name, ReadFileTool)
register_tool(WriteFileTool.name, WriteFileTool)
register_tool(RunCommandTool.name, RunCommandTool)
register_tool(ApplyPatchTool.name, ApplyPatchTool)
register_tool(SearchCodeTool.name, SearchCodeTool)
register_tool(GitStatusTool.name, GitStatusTool)
register_tool(GitDiffTool.name, GitDiffTool)
