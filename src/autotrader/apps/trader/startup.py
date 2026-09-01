"""What the loop needs before it can start, and what is missing.

The trader has never had a way to be started. Writing one meant deciding what
to do about the inputs that have no source: `BinanceLoopInputs` wants a
calendar, a fee schedule, order-flow thresholds, market pessimism and a
benchmark return series, and none of those is constructed anywhere outside a
unit test.

Two things this deliberately does not do.

It does not invent them. The values it would have to invent are tick sizes,
fees and account equity — "the operator's money, which no strategy may
invent", and a default is only a quieter way of inventing one. An order placed
against a made-up tick size is a real order.

It does not start anyway. The strategy already blocks without a regime, so a
loop wired from half-invented inputs would run, evaluate, and refuse on every
pass. A trader that looks like it is working is worse than one that will not
start, so the refusal happens here, by name, before anything runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.trader.composition import UnboundAccountError, bound_policy
from autotrader.execution.intents.models import AccountCandidate
from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.persistence.mysql.models.david_v6 import DavidV6ManifestRow
from autotrader.persistence.mysql.repositories.core import (
    CoreInstrumentRegistry,
    InstrumentNotRegisteredError,
    UnknownExchangeError,
)
from autotrader.strategies.david_v6.models import V6Market


class StartupRefusedError(RuntimeError):
    """Raised when the loop cannot be started, listing what is missing."""

    def __init__(self, missing: tuple[MissingInput, ...]) -> None:
        self.missing = missing
        super().__init__(
            "the loop cannot start; "
            + str(len(missing))
            + " inputs have no source:\n"
            + "\n".join(f"  - {item.name}: {item.reason}" for item in missing)
        )


@dataclass(frozen=True, slots=True)
class MissingInput:
    """One thing the loop needs and cannot get."""

    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResolvedAccount:
    """Everything the database can answer about which account to trade."""

    account: AccountCandidate
    binding_id: UUID
    instrument_id: UUID
    manifest_id: UUID
    policy_version_id: UUID
    market: V6Market


# The inputs `BinanceLoopInputs` requires that nothing in the system produces.
# Listed here rather than discovered one at a time at the call site, so the
# refusal names all of them at once and the list is a work item.
UNSOURCED_INPUTS = (
    MissingInput(
        name="range_efficiency",
        reason=(
            "a percentile the author's regime does not use; section 2.1 gives "
            "the regime as SMA 6/70/200 alone, and the quantity this ranks is "
            "defined in no document"
        ),
    ),
    MissingInput(
        name="atr_ratio",
        reason=(
            "as above: not part of the author's regime, and the lookback the "
            "ATR ratio is ranked against is unstated"
        ),
    ),
)

# Read from the venue rather than configured, so they are no longer on the
# list above: tick size, lot step, minimum quantity and minimum notional come
# from exchangeInfo, and the spread from the best bid and ask.
VENUE_SOURCED = (
    "tick_size",
    "quantity_step",
    "minimum_quantity",
    "minimum_notional",
    "spread",
    # Breadth is the share of the venue's own USDT perpetual contracts
    # advancing, ranked against its own history. Measured, not configured.
    "pessimism.breadth_percentile",
    "pessimism.volatility_percentile",
    # Measured daily from Deribit, and its history can only accumulate: the
    # public trade tape retains about a day and the volume endpoint is
    # rolling, so there is nothing to backfill from. It is no longer required
    # either - section 2.3 marks the quantitative triple as curriculum and the
    # detector the author used as a newspaper, so two measured components
    # decide, and this one joins them once it has sixty days.
    "pessimism.put_call_percentile",
    # Section 2.1's regime is SMA 6/70/200 on the instrument itself, and a
    # moving average is linear under rebasing, so the instrument's own daily
    # returns are the author's rule rather than a stand-in for it.
    "benchmark_returns",
    # Undisclosed by the author, telemetry only, and read by no decision.
    "order_flow_thresholds.ceros",
    # Section 19.1 rejects picking a contract count and section 22.5 gives the
    # crypto normalization instead: an aggregated event is big when it clears
    # the 0.995 quantile of the window's own events, extreme at 0.999. So the
    # two notionals an operator used to type are ranked rather than set.
    "order_flow_thresholds.big_trade_notional",
    # A session boundary placed by measurement: the close sits where BTCUSDT
    # liquidity thins, so the forced flat happens before the drought rather
    # than in it.
    "calendar",
    # Account-specific rather than published, so it is read signed from
    # /fapi/v1/commissionRate at the price the order would fill near, rather
    # than copied off a rate card that does not know this account's tier.
    "fee_schedule",
)


@dataclass(frozen=True, slots=True)
class OperatorFacts:
    """The money and the venue's units, which have to be stated.

    No defaults. Every field here is either the operator's capital or a fact
    about the instrument that decides the size and price of a real order, and
    a default would be this module guessing at one.
    """

    session_start_equity: Decimal
    current_equity: Decimal
    quantity_step: Decimal
    tick_size: Decimal
    spread: Decimal
    cost_per_unit: Decimal
    leverage: int

    def __post_init__(self) -> None:
        for name in (
            "session_start_equity",
            "current_equity",
            "quantity_step",
            "tick_size",
        ):
            value = getattr(self, name)
            if type(value) is not Decimal or value <= 0:
                raise ValueError(f"{name} must be a positive Decimal")
        for name in ("spread", "cost_per_unit"):
            value = getattr(self, name)
            if type(value) is not Decimal or value < 0:
                raise ValueError(f"{name} must be a non-negative Decimal")
        if type(self.leverage) is not int or self.leverage <= 0:
            raise ValueError("leverage must be a positive integer")


async def resolve_account(
    sessions: async_sessionmaker[AsyncSession],
    *,
    account_alias: str,
    market: V6Market,
    instrument_code: str,
    exchange_code: str,
) -> ResolvedAccount:
    """What the database knows, or a refusal naming what it does not.

    Each of these is a fact somebody entered through the back office. Reading
    them here rather than taking them as arguments is what makes the screens
    mean something: the loop trades the account the operator enabled, under the
    policy they bound, and nothing else.
    """
    missing: list[MissingInput] = []
    async with sessions() as session:
        found = (
            await session.execute(
                select(Account, Broker.code)
                .join(Broker, Broker.id == Account.broker_id)
                .where(Account.account_alias == account_alias)
            )
        ).first()
        if found is None:
            raise StartupRefusedError(
                (
                    MissingInput(
                        name="account",
                        reason=f"no account is stored under the alias {account_alias}",
                    ),
                )
            )
        account, broker_code = found
        if not account.enabled:
            missing.append(
                MissingInput(
                    name="account.enabled",
                    reason=(
                        "the account is disabled; enable it on the accounts "
                        "screen, which requires the second password"
                    ),
                )
            )
        binding = await session.scalar(
            select(ProviderAccountBinding).where(
                ProviderAccountBinding.account_id == account.id,
                ProviderAccountBinding.active.is_(True),
            )
        )
        if binding is None:
            missing.append(
                MissingInput(
                    name="provider_binding",
                    reason=(
                        "no active provider binding; bind one on the accounts screen"
                    ),
                )
            )
        manifest = await session.scalar(
            select(DavidV6ManifestRow).order_by(DavidV6ManifestRow.registered_at.desc())
        )
        if manifest is None:
            missing.append(
                MissingInput(
                    name="manifest",
                    reason=(
                        "no strategy manifest is registered; there is no exact "
                        "build for a decision to be recorded under"
                    ),
                )
            )
        try:
            instrument_id = await CoreInstrumentRegistry(session).resolve(
                exchange_code, instrument_code
            )
        except InstrumentNotRegisteredError:
            instrument_id = None
            missing.append(
                MissingInput(
                    name="instrument",
                    reason=f"{exchange_code}:{instrument_code} is not registered",
                )
            )
        except UnknownExchangeError:
            # A missing exchange is a missing reference seed, not a crash.
            # Letting it escape would defeat the point of this function.
            instrument_id = None
            missing.append(
                MissingInput(
                    name="exchange",
                    reason=(
                        f"{exchange_code} is not a seeded trading exchange; "
                        "the core reference rows have not been applied"
                    ),
                )
            )
        # Every value read out before the session goes away. A rollback
        # expires the instances, and a detached one raises on attribute access
        # rather than returning what it last held.
        #
        # The binding and the manifest were read after it. That only reaches
        # the operator once nothing else is missing, so the refusal path -
        # which is all this had ever done - never touched them, and the first
        # account complete enough to start crashed instead of starting.
        account_id = account.id
        environment = account.environment
        enabled = account.enabled
        binding_id = None if binding is None else binding.id
        manifest_id = None if manifest is None else manifest.id
        await session.rollback()

    policy_version_id: UUID | None = None
    try:
        policy_version_id = (
            await bound_policy(sessions, account_id=account_id, market=market)
        ).policy_version_id
    except UnboundAccountError:
        missing.append(
            MissingInput(
                name="risk_policy_binding",
                reason=(
                    "the account has no active risk policy binding for this "
                    "market; bind one on the risk policy screen"
                ),
            )
        )

    if missing:
        raise StartupRefusedError(tuple(missing))
    assert binding_id is not None
    assert manifest_id is not None
    assert instrument_id is not None
    assert policy_version_id is not None
    return ResolvedAccount(
        account=AccountCandidate(
            id=account_id,
            broker_code=broker_code,
            market_code=market.value,
            environment=environment,
            enabled=enabled,
            policy_key="david-v6",
            policy_active=True,
        ),
        binding_id=binding_id,
        instrument_id=instrument_id,
        manifest_id=manifest_id,
        policy_version_id=policy_version_id,
        market=market,
    )


def unsourced_inputs() -> tuple[MissingInput, ...]:
    """The inputs no part of the system produces yet.

    A function rather than a bare constant so a caller cannot mutate the list
    it is about to report.
    """
    return UNSOURCED_INPUTS


__all__ = (
    "UNSOURCED_INPUTS",
    "VENUE_SOURCED",
    "MissingInput",
    "OperatorFacts",
    "ResolvedAccount",
    "StartupRefusedError",
    "resolve_account",
    "unsourced_inputs",
)
