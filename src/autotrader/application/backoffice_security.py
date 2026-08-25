from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

_SECRET_REFERENCE_PATTERN = re.compile(
    r"secret://db/(?P<logical_name>[a-z0-9]+(?:-[a-z0-9]+)*)@active"
)
_LOGICAL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_MAX_LOGICAL_NAME_LENGTH = 128


@dataclass(frozen=True, slots=True)
class SecretReference:
    logical_name: str

    def __post_init__(self) -> None:
        if (
            type(self.logical_name) is not str
            or len(self.logical_name) > _MAX_LOGICAL_NAME_LENGTH
            or _LOGICAL_NAME_PATTERN.fullmatch(self.logical_name) is None
        ):
            raise ValueError("secret reference is invalid")

    @classmethod
    def parse(cls, value: str) -> SecretReference:
        if type(value) is not str:
            raise ValueError("secret reference is invalid")
        match = _SECRET_REFERENCE_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError("secret reference is invalid")
        return cls(logical_name=match.group("logical_name"))

    def __str__(self) -> str:
        return f"secret://db/{self.logical_name}@active"


class SecretValue:
    __slots__ = ("_mask", "_masked_value")

    def __init__(self, value: str) -> None:
        if type(value) is not str or not value:
            raise ValueError("secret value must be a non-empty string")
        try:
            plaintext = bytearray(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("secret value must be valid UTF-8") from None
        mask = bytearray(secrets.token_bytes(len(plaintext)))
        while not any(mask) or mask == plaintext:
            mask[:] = secrets.token_bytes(len(plaintext))
        try:
            self._mask = bytes(mask)
            self._masked_value = bytes(
                value_byte ^ mask_byte
                for value_byte, mask_byte in zip(plaintext, mask, strict=True)
            )
        finally:
            _wipe(plaintext)
            _wipe(mask)

    def get_secret_value(self) -> str:
        plaintext = bytearray(
            value_byte ^ mask_byte
            for value_byte, mask_byte in zip(
                self._masked_value, self._mask, strict=True
            )
        )
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("secret value is unavailable") from None
        finally:
            _wipe(plaintext)

    def __repr__(self) -> str:
        return "SecretValue('[REDACTED]')"

    def __str__(self) -> str:
        return self.__repr__()


def _wipe(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0
