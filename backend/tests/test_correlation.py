"""Epic 09: a run is traceable by one request id across API, queue, worker and stream."""

from __future__ import annotations

import pytest

from app.core.middleware import sanitize_request_id


@pytest.mark.parametrize("raw", ["abc", "req-1.2_3", "a" * 64, "  trimmed  "])
def test_sanitize_request_id_keeps_well_formed_ids(raw: str) -> None:
    assert sanitize_request_id(raw) == raw.strip()


@pytest.mark.parametrize("raw", [None, "", "   ", "a" * 65, "has space", "new\nline", "é", "a;b"])
def test_sanitize_request_id_replaces_bad_ids(raw: str | None) -> None:
    generated = sanitize_request_id(raw)
    assert generated != raw
    assert len(generated) == 32
    assert sanitize_request_id(generated) == generated
