from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LENGTH = 12
_FINGERPRINT_LENGTH = 32
_FINGERPRINT_DOMAIN = b"autotrader.backoffice.secret-fingerprint.v1"
_AAD_DOMAIN = b"autotrader.backoffice.secret-envelope.v1"


@dataclass(frozen=True, slots=True)
class SecretEnvelope:
    ciphertext: bytes
    nonce: bytes
    master_key_version: int
    fingerprint: bytes

    def __post_init__(self) -> None:
        if (
            type(self.ciphertext) is not bytes
            or len(self.ciphertext) < 16
            or type(self.nonce) is not bytes
            or len(self.nonce) != _NONCE_LENGTH
            or type(self.master_key_version) is not int
            or self.master_key_version <= 0
            or type(self.fingerprint) is not bytes
            or len(self.fingerprint) != _FINGERPRINT_LENGTH
        ):
            raise ValueError("secret envelope is invalid")


class MasterKeyRing:
    __slots__ = ("_current_version", "_keys")

    def __init__(
        self,
        *,
        current_key: bytes,
        current_version: int,
        previous_key: bytes | None = None,
        previous_version: int | None = None,
    ) -> None:
        _validate_key(current_key)
        _validate_version(current_version)
        if (previous_key is None) != (previous_version is None):
            raise ValueError("previous key and version must be provided together")
        if previous_key is not None and previous_version is not None:
            _validate_key(previous_key)
            _validate_version(previous_version)
            if previous_version == current_version:
                raise ValueError("master key versions must be distinct")

        keys = {current_version: current_key}
        if previous_key is not None and previous_version is not None:
            keys[previous_version] = previous_key
        self._current_version = current_version
        self._keys = keys

    def encrypt(self, *, plaintext: bytes, aad: bytes) -> SecretEnvelope:
        _validate_bytes(plaintext, name="plaintext")
        _validate_bytes(aad, name="AAD")
        version = self._current_version
        nonce = os.urandom(_NONCE_LENGTH)
        ciphertext = AESGCM(self._keys[version]).encrypt(
            nonce,
            plaintext,
            _authenticated_aad(version=version, aad=aad),
        )
        fingerprint = hashlib.sha256(_FINGERPRINT_DOMAIN + b"\x00" + plaintext).digest()
        return SecretEnvelope(
            ciphertext=ciphertext,
            nonce=nonce,
            master_key_version=version,
            fingerprint=fingerprint,
        )

    def decrypt(self, *, envelope: SecretEnvelope, aad: bytes) -> bytes:
        if type(envelope) is not SecretEnvelope:
            raise ValueError("secret envelope is invalid")
        envelope.__post_init__()
        _validate_bytes(aad, name="AAD")
        key = self._keys.get(envelope.master_key_version)
        if key is None:
            raise ValueError("secret envelope authentication failed") from None
        try:
            plaintext = AESGCM(key).decrypt(
                envelope.nonce,
                envelope.ciphertext,
                _authenticated_aad(
                    version=envelope.master_key_version,
                    aad=aad,
                ),
            )
        except InvalidTag:
            raise ValueError("secret envelope authentication failed") from None
        fingerprint = hashlib.sha256(_FINGERPRINT_DOMAIN + b"\x00" + plaintext).digest()
        if not hmac.compare_digest(fingerprint, envelope.fingerprint):
            del plaintext
            raise ValueError("secret envelope authentication failed") from None
        return plaintext


def _validate_key(key: object) -> None:
    if type(key) is not bytes:
        raise ValueError("master key must be bytes")
    if len(key) != 32:
        raise ValueError("master key must be exactly 32 bytes")


def _validate_version(version: object) -> None:
    if type(version) is not int or version <= 0:
        raise ValueError("master key version must be positive")


def _validate_bytes(value: object, *, name: str) -> None:
    if type(value) is not bytes or not value:
        raise ValueError(f"{name} must be non-empty bytes")


def _authenticated_aad(*, version: int, aad: bytes) -> bytes:
    return _AAD_DOMAIN + b"\x00" + str(version).encode("ascii") + b"\x00" + aad
