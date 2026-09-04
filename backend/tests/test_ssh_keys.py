from cryptography.hazmat.primitives import serialization

from app.services.ssh_keys import generate_ed25519_keypair


def test_generate_ed25519_keypair_roundtrip():
    pair = generate_ed25519_keypair("enigma-workspace-123 (test)")

    assert pair.private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert pair.private_key.endswith("\n")
    algorithm, blob, comment = pair.public_key.split(" ")
    assert algorithm == "ssh-ed25519"
    assert comment == "enigma-workspace-123-test"

    loaded = serialization.load_ssh_private_key(pair.private_key.encode(), password=None)
    derived = loaded.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    assert derived.decode() == f"{algorithm} {blob}"


def test_generate_ed25519_keypair_is_unique():
    assert generate_ed25519_keypair("a").public_key != generate_ed25519_keypair("a").public_key
