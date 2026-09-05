"""Server-side SSH deploy-key generation.

Keys are generated here (never uploaded by the user), the private half is stored Fernet-encrypted
via ``WorkspaceSecretCipher`` and only ever materialised inside the sandbox for the duration of a
single git command; the public half is what the user adds as a deploy key on their git host.
"""

import re
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_COMMENT_SAFE = re.compile(r"[^A-Za-z0-9_.@:-]+")


@dataclass(frozen=True)
class SshKeyPair:
    # OpenSSH PEM (``-----BEGIN OPENSSH PRIVATE KEY-----``), newline-terminated.
    private_key: str
    # Single ``ssh-ed25519 AAAA... <comment>`` line suitable for a deploy-key form.
    public_key: str


def generate_ed25519_keypair(comment: str) -> SshKeyPair:
    key = Ed25519PrivateKey.generate()
    private_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode("utf-8")
    public_key = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode("utf-8")
    )
    safe_comment = _COMMENT_SAFE.sub("-", comment).strip("-") or "enigma"
    return SshKeyPair(private_key=private_key, public_key=f"{public_key} {safe_comment}")
