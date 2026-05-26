#!/usr/bin/env python3
"""Run an AI code review for a GitHub pull request.

The script is designed for GitHub Actions. It reads the pull request event
payload, fetches changed files, asks OpenAI for a concise review through a
LangGraph workflow, and creates or updates one PR comment.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, NotRequired, TypedDict
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph


COMMENT_MARKER = "<!-- ai-code-review:openai -->"
MAX_FILES = 80
MAX_PATCH_CHARS = 55_000
GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")


class ReviewState(TypedDict):
    event: dict[str, Any]
    files: NotRequired[list[dict[str, Any]]]
    diff_payload: NotRequired[str]
    truncated: NotRequired[bool]
    review_text: NotRequired[str]


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def github_request(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> Any:
    url = f"{GITHUB_API_URL}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
            return json.loads(response_body) if response_body else None
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8")
        raise RuntimeError(f"GitHub API request failed ({exc.code}) {method} {path}: {error_body}") from exc


def github_paginate(path: str, token: str, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    base_query = dict(query or {})

    while True:
        page_query = {**base_query, "per_page": 100, "page": page}
        page_items = github_request("GET", path, token, query=page_query)
        if not page_items:
            return items
        items.extend(page_items)
        if len(page_items) < 100:
            return items
        page += 1


def load_event() -> dict[str, Any]:
    event_path = require_env("GITHUB_EVENT_PATH")
    with open(event_path, encoding="utf-8") as event_file:
        return json.load(event_file)


def fetch_files(state: ReviewState) -> ReviewState:
    event = state["event"]
    pull = event["pull_request"]
    repo = event["repository"]
    token = require_env("GITHUB_TOKEN")
    files_path = f"/repos/{repo['owner']['login']}/{repo['name']}/pulls/{pull['number']}/files"
    return {**state, "files": github_paginate(files_path, token)}


def build_diff_payload(state: ReviewState) -> ReviewState:
    event = state["event"]
    pull = event["pull_request"]
    repo = event["repository"]
    files = state.get("files", [])
    patch_chars = 0
    truncated = False
    reviewed_files: list[str] = []

    for file_info in files[:MAX_FILES]:
        header = "\n".join(
            [
                f"File: {file_info['filename']}",
                f"Status: {file_info['status']}",
                f"Changes: +{file_info['additions']} -{file_info['deletions']}",
            ]
        )
        patch = file_info.get("patch") or "[No patch available from GitHub API]"
        remaining = MAX_PATCH_CHARS - patch_chars

        if remaining <= 0:
            truncated = True
            break

        if len(patch) > remaining:
            selected_patch = f"{patch[:remaining]}\n[Patch truncated]"
            truncated = True
        else:
            selected_patch = patch

        patch_chars += len(selected_patch)
        reviewed_files.append(f"{header}\n\n{selected_patch}")

        if truncated:
            break

    if len(files) > MAX_FILES:
        truncated = True

    diff_payload = "\n\n".join(
        [
            f"Repository: {repo['full_name']}",
            f"PR #{pull['number']}: {pull['title']}",
            f"Author: {pull['user']['login']}",
            f"Base: {pull['base']['ref']}",
            f"Head: {pull['head']['ref']}",
            f"Head SHA: {pull['head']['sha']}",
            f"PR Body:\n{pull.get('body') or '[No PR body provided]'}",
            f"Changed files: {len(files)}",
            (
                "Note: The diff was truncated. Mention that review coverage was limited if relevant."
                if truncated
                else "Note: The full available diff payload was included."
            ),
            "",
            "\n\n---\n\n".join(reviewed_files),
        ]
    )
    return {**state, "diff_payload": diff_payload, "truncated": truncated}


def review_with_openai(state: ReviewState) -> ReviewState:
    require_env("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-5.4")
    llm = ChatOpenAI(model=model, max_tokens=1800)
    messages = [
        SystemMessage(
            content="\n".join(
                [
                    "You are an expert code reviewer.",
                    "Review the pull request diff and return concise, actionable findings.",
                    "Prioritize correctness bugs, security issues, data loss, race conditions, broken tests, and behavioral regressions.",
                    "Include file paths and line references when possible.",
                    "Avoid purely stylistic comments unless they materially affect maintainability or correctness.",
                    "If there are no actionable findings, say: No actionable issues found.",
                    "Do not invent issues that are not supported by the diff.",
                ]
            )
        ),
        HumanMessage(content=state["diff_payload"]),
    ]
    response = llm.invoke(messages)
    review_text = str(response.content).strip() or "No actionable issues found."
    return {**state, "review_text": review_text}


def upsert_comment(state: ReviewState) -> ReviewState:
    event = state["event"]
    pull = event["pull_request"]
    repo = event["repository"]
    token = require_env("GITHUB_TOKEN")
    model = os.getenv("OPENAI_MODEL", "gpt-5.4")
    comments_path = f"/repos/{repo['owner']['login']}/{repo['name']}/issues/{pull['number']}/comments"
    comment_body = "\n".join(
        [
            COMMENT_MARKER,
            "## AI Code Review",
            "",
            f"Reviewed commit: `{pull['head']['sha']}`",
            f"Model: `{model}`",
            "",
            state["review_text"],
        ]
    )

    comments = github_paginate(comments_path, token)
    existing_comment = next(
        (
            comment
            for comment in comments
            if comment.get("user", {}).get("type") == "Bot"
            and COMMENT_MARKER in (comment.get("body") or "")
        ),
        None,
    )

    if existing_comment:
        github_request(
            "PATCH",
            f"/repos/{repo['owner']['login']}/{repo['name']}/issues/comments/{existing_comment['id']}",
            token,
            {"body": comment_body},
        )
    else:
        github_request("POST", comments_path, token, {"body": comment_body})

    return state


def build_graph():
    graph = StateGraph(ReviewState)
    graph.add_node("fetch_files", fetch_files)
    graph.add_node("build_diff_payload", build_diff_payload)
    graph.add_node("review_with_openai", review_with_openai)
    graph.add_node("upsert_comment", upsert_comment)

    graph.set_entry_point("fetch_files")
    graph.add_edge("fetch_files", "build_diff_payload")
    graph.add_edge("build_diff_payload", "review_with_openai")
    graph.add_edge("review_with_openai", "upsert_comment")
    graph.add_edge("upsert_comment", END)
    return graph.compile()


def main() -> int:
    event = load_event()
    workflow = build_graph()
    workflow.invoke({"event": event})
    print("AI code review comment created or updated.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"AI code review failed: {exc}", file=sys.stderr)
        raise
