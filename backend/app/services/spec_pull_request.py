"""The pull request body for a completed Spec Kit workflow (Verifiable Agent Loop, Plan 4).

The PR carries what a reviewer needs to judge the change without opening Enigma: the
feature, links to its Spec Kit artifacts on the branch, a short spec excerpt and a summary of
the approved verification evidence. It never carries test or command output, nor the
observation's text: either can hold secrets or internal addresses, and the PR may be public.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime
from urllib.parse import quote, urlparse

from app.services.github_client import parse_owner_repo

# GitHub rejects bodies over 65,536 characters.
MAX_BODY_CHARS = 60_000
SPEC_EXCERPT_CHARS = 3_000
COMMAND_CHARS = 200
TRUNCATED = "\n\n…(truncated)"
# Bytes read from the sandbox for the excerpt (UTF-8 may use several bytes per char).
SPEC_READ_BYTES = SPEC_EXCERPT_CHARS * 4


def confined_read_command(rel_path: str, limit: int) -> str:
    """A shell command printing at most ``limit`` bytes of a regular file inside the cwd.

    The file is agent-written, so it may be a symlink (or sit under a symlinked directory)
    pointing at credentials elsewhere in the sandbox; anything resolving outside the cwd is
    refused, so such a file can never reach a possibly public PR.
    """
    return (
        f"p=$(readlink -f -- {shlex.quote(rel_path)}) && "
        'case "$p" in "$(pwd -P)"/*) [ -f "$p" ] && '
        f'head -c {int(limit)} -- "$p" ;; *) exit 1 ;; esac'
    )


# What `specify init` adds to a repository (the generic integration, plus the Claude layouts
# of repositories initialised before it). Feature artifacts under specs/ are not scaffold.
SCAFFOLD_PATHS = (
    ".specify",
    ".enigma",
    "AGENTS.md",
    ".claude/skills/speckit-*",
    ".claude/commands/speckit.*",
)


def scaffold_diff_command(base_branch: str) -> str:
    """Lists the scaffold files the branch adds or changes relative to the remote base.

    The first Spec Kit PR in a repository carries the `chore: initialize Spec Kit` commit,
    because Enigma commits the scaffold to the sandbox's base branch, which is never pushed.
    """
    paths = " ".join(shlex.quote(path) for path in SCAFFOLD_PATHS)
    return f"git diff --name-only {shlex.quote(f'origin/{base_branch}')}...HEAD -- {paths}"


def _scaffold_section(files: int) -> list[str]:
    return [
        "",
        "### Spec Kit scaffold",
        f"This is the first Spec Kit change in this repository, so it also adds {files} files "
        "of Spec Kit's own scaffold (`.specify/`, `.enigma/commands/` and a block in "
        "`AGENTS.md`), in the commit `chore: initialize Spec Kit (SDD)`. They are Spec Kit's "
        "templates, scripts and phase guidance, not part of this feature, and can be skimmed. "
        "Later Spec Kit PRs will not carry them once this one is merged.",
    ]


def _artifact_base(repo_url: str, branch: str, spec_dir: str) -> str:
    owner, repo = parse_owner_repo(repo_url)
    host = urlparse(repo_url).hostname or "github.com"
    return f"https://{host}/{owner}/{repo}/blob/{quote(branch)}/{spec_dir}"


def _code_span(text: str) -> str:
    """One table-safe line in a Markdown code span."""
    line = " ".join(text.split())[:COMMAND_CHARS].replace("|", "\\|")
    return f"`` {line} ``" if "`" in line else f"`{line}`"


def _last(evidence, kind: str):
    matching = [item for item in evidence if item.kind == kind]
    return matching[-1] if matching else None


def _verification(run, approved_at: datetime | None) -> list[str]:
    if run is None:
        return ["Not verified: this workflow had no verification criteria."]
    evidence = sorted(run.evidence, key=lambda item: item.created_at)
    test = _last(evidence, "test")
    observation = _last(evidence, "observation")
    rows = [
        "| | |",
        "|---|---|",
        f"| Test command | {_code_span((run.criteria or {}).get('test_command') or '')} |",
        f"| Last test | {test.status if test else 'not run'} |",
        f"| Fix attempts | {run.iteration} |",
        f"| Observation | {observation.status if observation else 'not run'} |",
    ]
    if approved_at is not None:
        rows.append(f"| Evidence approved | {approved_at.date().isoformat()} |")
    return rows


def build_pr_body(
    workflow,
    *,
    repo_url: str,
    spec_excerpt: str | None,
    run,
    approved_at: datetime | None,
    scaffold_files: int = 0,
) -> str:
    base = _artifact_base(repo_url, workflow.branch, workflow.spec_dir)
    lines = [
        workflow.description.strip(),
        "",
        "### Spec Kit artifacts",
        *(f"- [{name}]({base}/{name})" for name in ("spec.md", "plan.md", "tasks.md")),
    ]
    if spec_excerpt and spec_excerpt.strip():
        excerpt = spec_excerpt.strip()
        if len(excerpt) > SPEC_EXCERPT_CHARS:
            excerpt = excerpt[:SPEC_EXCERPT_CHARS] + TRUNCATED
        # Agent-written: a code block keeps its @mentions and #refs from notifying anyone.
        longest = max((len(run) for run in re.findall(r"`+", excerpt)), default=0)
        fence = "`" * max(4, longest + 1)
        lines += [
            "",
            "<details><summary>Spec excerpt</summary>",
            "",
            f"{fence}markdown",
            excerpt,
            fence,
            "",
            "</details>",
        ]
    if scaffold_files > 0:
        lines += _scaffold_section(scaffold_files)
    lines += [
        "",
        "### Verification",
        *_verification(run, approved_at),
        "",
        f"_Opened by Enigma from Spec Kit workflow `{workflow.feature_slug}`._",
    ]
    body = "\n".join(lines)
    if len(body) > MAX_BODY_CHARS:
        body = body[: MAX_BODY_CHARS - len(TRUNCATED)] + TRUNCATED
    return body
