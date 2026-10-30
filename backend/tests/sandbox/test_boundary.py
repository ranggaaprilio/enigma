"""Acceptance guards for the provider boundary (local-docker-runtime Epic 01)."""

from __future__ import annotations

import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2] / "app"
SANDBOX_DIR = APP_DIR / "services" / "sandbox"

_SDK_IMPORT = re.compile(r"^\s*(from|import)\s+(daytona|docker)\b")
_LEAK = re.compile(r"\bget_sandbox\(|\bsandbox\.fs\.|\bsandbox\.git\.|\bdaytona_sandbox_id\b")


def _offenders(pattern: re.Pattern[str], *, exempt: set[Path]) -> list[str]:
    found = []
    for path in APP_DIR.rglob("*.py"):
        if SANDBOX_DIR in path.parents or path in exempt:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                found.append(f"{path.relative_to(APP_DIR.parent)}:{lineno}: {line.strip()}")
    return found


def test_provider_sdks_are_imported_only_inside_the_sandbox_package() -> None:
    assert _offenders(_SDK_IMPORT, exempt={APP_DIR / "services" / "daytona_runtime.py"}) == []


def test_no_consumer_reaches_past_the_runtime_interface() -> None:
    assert _offenders(_LEAK, exempt={APP_DIR / "models" / "workspace.py"}) == []
