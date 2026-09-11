from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from app.services.preview_capability import (
    CAPABILITY_LABEL_LENGTH,
    InvalidPreviewCapabilityError,
    capability_from_host,
    create_preview_proxy_url,
    decode_preview_capability,
    encode_preview_capability,
    is_capability_label,
    proxy_host_label,
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


# ---------------------------------------------------------------------------
# Epic 13: proxy_host_label / is_capability_label -- the refactor extracting
# capability_from_host's hardening for reuse by alias resolution.
# ---------------------------------------------------------------------------


def test_capability_from_host_is_a_thin_wrapper_over_proxy_host_label() -> None:
    """No behaviour change: the exact same result for the exact same input."""
    for host in (
        "abc.preview.example.com",
        "ABC.PREVIEW.EXAMPLE.COM",
        "abc.preview.example.com.",
        "abc.preview.example.com:8001",
    ):
        assert capability_from_host(host, base_url="https://preview.example.com") == (
            proxy_host_label(host, base_url="https://preview.example.com")
        )


@pytest.mark.parametrize(
    "host",
    [
        "abc.preview.example.com.",  # trailing dot
        "ABC.PREVIEW.EXAMPLE.COM",  # uppercase
        "abc.preview.example.com:8001",  # explicit port
    ],
)
def test_proxy_host_label_normalizes_trailing_dot_case_and_port(host: str) -> None:
    assert proxy_host_label(host, base_url="https://preview.example.com") == "abc"


@pytest.mark.parametrize(
    "host",
    [
        "abc.def.preview.example.com",  # multi-label subdomain
        "preview.example.com",  # no label at all
        ".preview.example.com",  # empty label
        "abc.attacker.example.com",  # wrong suffix
        "abc.preview.example.com.evil.com",  # suffix trick
    ],
)
def test_proxy_host_label_rejects_multi_label_and_wrong_suffix(host: str) -> None:
    with pytest.raises(InvalidPreviewCapabilityError):
        proxy_host_label(host, base_url="https://preview.example.com")


def test_is_capability_label_accepts_a_freshly_minted_capability() -> None:
    encoded = encode_preview_capability(
        uuid4(),
        5173,
        expires_at=datetime(2026, 6, 12, 12, 0, tzinfo=UTC),
        secret_key=SECRET,
    )
    assert len(encoded) == CAPABILITY_LABEL_LENGTH
    assert is_capability_label(encoded) is True


@pytest.mark.parametrize(
    "label",
    [
        "web-api",  # contains a hyphen -- not in the base32 alphabet
        "a" * 62,  # one short of the required length
        "a" * 64,  # one over
        "A" * 63,  # uppercase (capabilities are always lowercased)
        "1" * 63,  # '1' is not in the base32 alphabet [a-z2-7]
    ],
)
def test_is_capability_label_rejects_non_capability_shapes(label: str) -> None:
    assert is_capability_label(label) is False
