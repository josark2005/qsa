from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _seed_from_secret(secret: str) -> bytes:
    if not secret:
        raise ValueError("QQ bot secret is empty")
    raw = secret.encode("utf-8")
    return (raw * (32 // len(raw) + 1))[:32]


def _private_key(secret: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_seed_from_secret(secret))


def sign_validation(secret: str, event_ts: str, plain_token: str) -> str:
    message = f"{event_ts}{plain_token}".encode("utf-8")
    return _private_key(secret).sign(message).hex()


def verify_callback(
    secret: str,
    timestamp: str,
    body: bytes,
    signature_hex: str,
) -> bool:
    if not secret or not timestamp or not signature_hex:
        return False
    try:
        signature = bytes.fromhex(signature_hex)
        _private_key(secret).public_key().verify(
            signature,
            timestamp.encode("utf-8") + body,
        )
    except (InvalidSignature, ValueError):
        return False
    return True
