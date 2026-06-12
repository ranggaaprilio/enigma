import base64
import hashlib
import hmac
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
    parsed = _parse_proxy_base_url(base_url)
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


def capability_from_host(host: str, *, base_url: str) -> str:
    parsed = _parse_proxy_base_url(base_url)
    hostname = host.split(":", 1)[0].rstrip(".").lower()
    suffix = f".{parsed.hostname.lower()}"
    if not hostname.endswith(suffix):
        raise InvalidPreviewCapabilityError("Invalid preview proxy host")
    capability = hostname[: -len(suffix)]
    if not capability or "." in capability:
        raise InvalidPreviewCapabilityError("Invalid preview proxy host")
    return capability


def _parse_proxy_base_url(base_url: str) -> SplitResult:
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
        raise ValueError("DAYTONA_PREVIEW_PROXY_BASE_URL must be an HTTP origin")
    return parsed
