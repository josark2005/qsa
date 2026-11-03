from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

# Used to keep login timing similar when the user does not exist.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(24))


class PasswordPolicyError(ValueError):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def verify_dummy_password(password: str) -> None:
    try:
        _hasher.verify(_DUMMY_HASH, password)
    except (VerifyMismatchError, InvalidHashError):
        pass


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def generate_password(length: int = 16) -> str:
    return secrets.token_urlsafe(length)


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_password(password: str, min_length: int) -> None:
    if len(password) < min_length:
        raise PasswordPolicyError(f"密码长度至少为 {min_length} 位")
    if len(password) > 256:
        raise PasswordPolicyError("密码长度不能超过 256 位")
