from __future__ import annotations

import pytest

from app.core.security import (
    PasswordPolicyError,
    hash_password,
    validate_password,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


def test_password_min_length() -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password("short", 12)
    validate_password("long-enough-password", 12)
