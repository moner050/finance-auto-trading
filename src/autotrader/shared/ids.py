from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7


def new_uuid7() -> UUID:
    return uuid7()


def uuid7_from_sha256(timestamp: datetime, digest: bytes) -> UUID:
    if (
        type(timestamp) is not datetime
        or timestamp.tzinfo is not UTC
        or timestamp.utcoffset() != timedelta(0)
        or timestamp.microsecond
    ):
        raise ValueError("timestamp must be whole-second UTC")
    if type(digest) is not bytes or len(digest) != 32:
        raise ValueError("digest must be SHA-256")
    delta = timestamp - datetime(1970, 1, 1, tzinfo=UTC)
    timestamp_ms = (delta.days * 86_400 + delta.seconds) * 1_000
    if not 0 <= timestamp_ms < 2**48:
        raise ValueError("timestamp is outside UUIDv7 range")
    value = bytearray(timestamp_ms.to_bytes(6, "big") + digest[:10])
    value[6] = (value[6] & 0x0F) | 0x70
    value[8] = (value[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(value))


def uuid_to_binary(value: UUID) -> bytes:
    _require_uuid7(value)
    return value.bytes


def uuid_from_binary(value: bytes) -> UUID:
    result = UUID(bytes=value)
    _require_uuid7(result)
    return result


def _require_uuid7(value: UUID) -> None:
    if value.version != 7:
        raise ValueError("UUIDv7 is required")
