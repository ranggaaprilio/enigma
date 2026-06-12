from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from app.services.preview_capability import (
    InvalidPreviewCapabilityError,
    capability_from_host,
    create_preview_proxy_url,
    decode_preview_capability,
    encode_preview_capability,
)

SECRET = "preview-capability-secret"


def test_preview_capability_round_trip_uses_single_dns_label() -> None:
    session_id = uuid4()
    expires_at = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)

    encoded = encode_preview_capability(
        session_id,
        5173,
        expires_at=expires_at,
        secret_key=SECRET,
    )
    decoded = decode_preview_capability(
        encoded,
        secret_key=SECRET,
        now=expires_at - timedelta(seconds=1),
    )

    assert len(encoded) == 63
    assert decoded.preview_session_id == session_id
    assert decoded.port == 5173
    assert decoded.expires_at == expires_at


def test_preview_capability_rejects_tampering_and_expiry() -> None:
    expires_at = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    encoded = encode_preview_capability(
        uuid4(),
        5173,
        expires_at=expires_at,
        secret_key=SECRET,
    )
    replacement = "a" if encoded[-1] != "a" else "b"

    with pytest.raises(InvalidPreviewCapabilityError):
        decode_preview_capability(
            f"{encoded[:-1]}{replacement}",
            secret_key=SECRET,
            now=expires_at - timedelta(seconds=1),
        )

    with pytest.raises(InvalidPreviewCapabilityError, match="expired"):
        decode_preview_capability(
            encoded,
            secret_key=SECRET,
            now=expires_at + timedelta(seconds=1),
        )


def test_preview_proxy_url_and_host_parser_support_local_and_production_origins() -> None:
    session_id = uuid4()
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)

    local_url = create_preview_proxy_url(
        session_id,
        5173,
        base_url="http://localhost:8001",
        secret_key=SECRET,
        ttl_seconds=3600,
        now=now,
    )
    production_url = create_preview_proxy_url(
        session_id,
        5173,
        base_url="https://preview.example.com",
        secret_key=SECRET,
        ttl_seconds=3600,
        now=now,
    )

    local = urlsplit(local_url)
    production = urlsplit(production_url)
    assert local.hostname is not None
    assert local.hostname.endswith(".localhost")
    assert local.port == 8001
    assert production.hostname is not None
    assert production.hostname.endswith(".preview.example.com")
    assert capability_from_host(
        local.netloc,
        base_url="http://localhost:8001",
    ) == local.hostname.split(".", 1)[0]

    with pytest.raises(InvalidPreviewCapabilityError):
        capability_from_host(
            "capability.attacker.example",
            base_url="https://preview.example.com",
        )
