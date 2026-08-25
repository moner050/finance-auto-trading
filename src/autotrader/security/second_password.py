from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

_PASSWORD_HASHER = PasswordHasher(type=Type.ID)
_ARGON2ID_PREFIX = "$argon2id$"


def hash_second_password(password: str) -> str:
    _validate_password(password)
    return _PASSWORD_HASHER.hash(password)


def verify_second_password(verifier: str, password: str) -> bool:
    if (
        type(verifier) is not str
        or not verifier.startswith(_ARGON2ID_PREFIX)
        or type(password) is not str
        or not password
    ):
        return False
    try:
        if not _PASSWORD_HASHER.verify(verifier, password):
            return False
        _PASSWORD_HASHER.check_needs_rehash(verifier)
        return True
    except VerificationError, InvalidHashError:
        return False


def _validate_password(password: object) -> None:
    if type(password) is not str or not password:
        raise ValueError("second password must be a non-empty string")
