from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class KisActiveContract:
    evidence_id: UUID
    data_source_id: UUID
    instrument_id: UUID
    provider_contract_code: str
    provider_exchange_code: str
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("evidence_id", "data_source_id", "instrument_id"):
            value = getattr(self, name)
            if not isinstance(value, UUID) or value.version != 7:
                raise ValueError(f"{name} must be UUIDv7")
        if (
            not self.provider_contract_code
            or not self.provider_contract_code.isascii()
            or not self.provider_contract_code.isalnum()
            or self.provider_contract_code != self.provider_contract_code.upper()
        ):
            raise ValueError(
                "provider_contract_code must be uppercase ASCII alphanumeric"
            )
        if (
            not self.provider_exchange_code
            or not self.provider_exchange_code.isascii()
            or not self.provider_exchange_code.isalnum()
            or self.provider_exchange_code != self.provider_exchange_code.upper()
        ):
            raise ValueError(
                "provider_exchange_code must be uppercase ASCII alphanumeric"
            )
        if (
            self.expires_at.tzinfo is not UTC
            or self.expires_at.utcoffset() != UTC.utcoffset(self.expires_at)
        ):
            raise ValueError("expires_at must be UTC-aware")


class KisContractMasterReader(Protocol):
    async def load_active(
        self, *, evidence_id: UUID, now: datetime
    ) -> KisActiveContract: ...
