import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 600_000
SALT_BYTES = 16


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return (
        f"{PASSWORD_HASH_ALGORITHM}${PASSWORD_HASH_ITERATIONS}"
        f"${_b64_encode(salt)}${_b64_encode(password_hash)}"
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt, stored_hash = password_hash.split("$", 3)
        if algorithm != PASSWORD_HASH_ALGORITHM:
            return False

        candidate_hash = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64_decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(_b64_encode(candidate_hash), stored_hash)
    except (TypeError, ValueError):
        return False


def create_session_token(user_id: str, secret_key: str, ttl_seconds: int) -> str:
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    payload = {
        "sub": user_id,
        "exp": int(expires_at.timestamp()),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_segment = _b64_encode(payload_json)
    signature = hmac.new(
        secret_key.encode("utf-8"),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_segment}.{_b64_encode(signature)}"


def verify_session_token(token: str, secret_key: str) -> dict[str, Any] | None:
    try:
        payload_segment, signature_segment = token.split(".", 1)
        expected_signature = hmac.new(
            secret_key.encode("utf-8"),
            payload_segment.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64_encode(expected_signature), signature_segment):
            return None

        payload = json.loads(_b64_decode(payload_segment))
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None

    if expires_at < int(datetime.now(UTC).timestamp()):
        return None

    return payload
