import base64
import hashlib
import hmac
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import UUID

CAPABILITY_VERSION = 1
SIGNATURE_BYTES = 16
PAYLOAD_FORMAT = "!B16sHI"
PAYLOAD_BYTES = struct.calcsize(PAYLOAD_FORMAT)


class InvalidPreviewCapabilityError(Exception):
    pass


@dataclass(frozen=True)
class PreviewCapability:
    preview_session_id: UUID
    port: int
    expires_at: datetime


def create_preview_proxy_url(
    preview_session_id: UUID,
    port: int,
    *,
    base_url: str,
    secret_key: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    current_time = now or datetime.now(UTC)
    expires_at = current_time + timedelta(seconds=ttl_seconds)
    capability = encode_preview_capability(
        preview_session_id,
        port,
        expires_at=expires_at,
        secret_key=secret_key,
    )
    parsed = parse_proxy_base_url(base_url)
    host = f"{capability}.{parsed.hostname}"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, "/", "", ""))


def encode_preview_capability(
    preview_session_id: UUID,
    port: int,
    *,
    expires_at: datetime,
    secret_key: str,
) -> str:
    if not 1 <= port <= 65535:
        raise ValueError("Preview port must be between 1 and 65535")
    payload = struct.pack(
        PAYLOAD_FORMAT,
        CAPABILITY_VERSION,
        preview_session_id.bytes,
        port,
        int(expires_at.timestamp()),
    )
    signature = hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    encoded = base64.b32encode(payload + signature[:SIGNATURE_BYTES]).decode("ascii")
    return encoded.rstrip("=").lower()


def decode_preview_capability(
    value: str,
    *,
    secret_key: str,
    now: datetime | None = None,
) -> PreviewCapability:
    try:
        padded = value.upper() + "=" * (-len(value) % 8)
        decoded = base64.b32decode(padded, casefold=True)
        canonical = base64.b32encode(decoded).decode("ascii").rstrip("=").lower()
        payload = decoded[:PAYLOAD_BYTES]
        signature = decoded[PAYLOAD_BYTES:]
        version, session_bytes, port, expires_at = struct.unpack(PAYLOAD_FORMAT, payload)
    except (ValueError, struct.error) as error:
        raise InvalidPreviewCapabilityError("Invalid preview capability") from error

    expected = hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha256).digest()
    if (
        canonical != value.lower()
        or version != CAPABILITY_VERSION
        or len(signature) != SIGNATURE_BYTES
        or not hmac.compare_digest(signature, expected[:SIGNATURE_BYTES])
    ):
        raise InvalidPreviewCapabilityError("Invalid preview capability")

    expiry = datetime.fromtimestamp(expires_at, UTC)
    if expiry < (now or datetime.now(UTC)):
        raise InvalidPreviewCapabilityError("Preview capability has expired")
    return PreviewCapability(
        preview_session_id=UUID(bytes=session_bytes),
        port=port,
        expires_at=expiry,
    )


def proxy_host_label(host: str, *, base_url: str) -> str:
    """The single DNS label directly under the proxy's own hostname -- e.g. ``capability`` (or,
    from Epic 13 onward, an alias) out of ``capability.preview.example.com``.

    Hardens against trailing-dot, uppercase and multi-label tricks: the ``Host`` header is
    lowercased and its trailing dot stripped before the suffix check, and a remainder containing
    a dot (a second, nested subdomain) is rejected outright. Epic 13's alias resolution reuses
    this exact function -- unmodified -- rather than duplicating the hardening.
    """
    parsed = parse_proxy_base_url(base_url)
    hostname, _, port = host.partition(":")
    # Anything after the first `:` must be a plain port: the raw Host is forwarded upstream as
    # `X-Forwarded-Host`, so `<label>.<base>:x.evil.com` must not pass as a preview host.
    if port and not (port.isascii() and port.isdigit()):
        raise InvalidPreviewCapabilityError("Invalid preview proxy host")
    hostname = hostname.rstrip(".").lower()
    suffix = f".{parsed.hostname.lower()}"
    if not hostname.endswith(suffix):
        raise InvalidPreviewCapabilityError("Invalid preview proxy host")
    label = hostname[: -len(suffix)]
    if not label or "." in label:
        raise InvalidPreviewCapabilityError("Invalid preview proxy host")
    return label


# A capability label is exactly 63 base32 characters: a 23-byte payload (1-byte version +
# 16-byte session UUID + uint16 port + uint32 expiry) plus a 16-byte truncated HMAC, base32
# encoded over [A-Z2-7] and lowercased. `-` (used by every alias, Epic 13) is not in that
# alphabet, and no alias can reach 63 characters (its format caps it at 62) -- so the two label
# shapes are mutually exclusive by construction, not by checking one first.
CAPABILITY_LABEL_LENGTH = 63
_CAPABILITY_LABEL_PATTERN = re.compile(r"^[a-z2-7]{63}$")


def is_capability_label(label: str) -> bool:
    return bool(_CAPABILITY_LABEL_PATTERN.match(label))


def capability_from_host(host: str, *, base_url: str) -> str:
    return proxy_host_label(host, base_url=base_url)


def parse_proxy_base_url(base_url: str) -> SplitResult:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PREVIEW_PROXY_BASE_URL must be an HTTP origin")
    return parsed
