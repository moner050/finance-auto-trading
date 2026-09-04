"""What the protection service does when a position cannot be protected.

`BinanceUsdmProtectionService` reaches for these two before and after it
closes a position it could not put a stop behind. They are separate on
purpose, and the order the service calls them in matters.

**Stopping new exposure is not halting.** A halt would also stop the
protective order from being placed or filled, which is the opposite of what
an unprotected position needs. So the first action raises the kill switch
only as far as `BLOCK_NEW_EXPOSURE`: nothing new opens, everything that
closes still can.

**Halting comes after the position is out.** The service closes first and
refuses second, because the position leaving is the urgent half and a halted
account cannot do it.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.execution.controls.models import KillSwitchLevel
from autotrader.persistence.mysql.models.operations import OpsTradingControl


@dataclass(frozen=True, slots=True)
class MySqlProtectionSafetyActions:
    """`BinanceUsdmProtectionSafetyActions` over the operator's own controls.

    The binding is checked rather than used to scope the write. A control row
    covers the account, not one provider registration under it, so scoping by
    binding would leave the other registrations armed while this one is in
    trouble - and they trade the same money.
    """

    sessions: async_sessionmaker[AsyncSession]
    binding_id: UUID

    async def cancel_entry_and_adds(self, binding_id: UUID) -> None:
        """Stop opening exposure, and only that."""
        self._require_own(binding_id)
        await self._raise_to(KillSwitchLevel.BLOCK_NEW_EXPOSURE)

    async def halt_account(self, binding_id: UUID, reason: str) -> None:
        """Stop everything, and disarm, because a halt that left the account
        armed would be a halt in name only."""
        self._require_own(binding_id)
        if not reason.strip():
            # A halt with no reason is one nobody can act on later.
            raise ValueError("halting an account needs a reason")
        await self._raise_to(KillSwitchLevel.EMERGENCY, disarm=True)

    def _require_own(self, binding_id: UUID) -> None:
        if binding_id != self.binding_id:
            # Acting on another binding's trouble would halt an account this
            # was not built for.
            raise ValueError("these safety actions answer for one binding only")

    async def _raise_to(self, level: KillSwitchLevel, *, disarm: bool = False) -> None:
        """Only ever upward. A level already stronger stays."""
        async with self.sessions() as session:
            controls = (await session.scalars(select(OpsTradingControl))).all()
            for control in controls:
                if _rank(control.kill_switch_level) < _rank(level.value):
                    control.kill_switch_level = level.value
                    control.row_version += 1
                elif disarm and control.armed:
                    control.row_version += 1
                if disarm:
                    control.armed = False
            await session.commit()


_ORDER = (
    KillSwitchLevel.NONE.value,
    KillSwitchLevel.BLOCK_NEW_EXPOSURE.value,
    KillSwitchLevel.EMERGENCY.value,
)


def _rank(level: str) -> int:
    try:
        return _ORDER.index(level)
    except ValueError:
        # An unknown level is treated as the strongest, so a row this code
        # does not understand is never quietly weakened.
        return len(_ORDER)


__all__ = ("MySqlProtectionSafetyActions",)
