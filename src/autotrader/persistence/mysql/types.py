from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import BINARY, DateTime
from sqlalchemy.types import TypeDecorator


class UuidBinary(TypeDecorator[UUID]):
    impl = BINARY(16)
    cache_ok = True

    def process_bind_param(self, value: UUID | None, dialect: object) -> bytes | None:
        del dialect
        if value is None:
            return None
        self._require_v7(value)
        return value.bytes

    def process_result_value(self, value: bytes | None, dialect: object) -> UUID | None:
        del dialect
        if value is None:
            return None
        result = UUID(bytes=value)
        self._require_v7(result)
        return result

    @staticmethod
    def _require_v7(value: UUID) -> None:
        if value.version != 7:
            raise ValueError("UUIDv7 is required")


class UtcDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware UTC datetime is required")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        del dialect
        return None if value is None else value.replace(tzinfo=UTC)
