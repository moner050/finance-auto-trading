"""What the risk policy screen shows, and how two versions differ.

These fractions decide how much money moves, so the screen shows them as they
are stored rather than reformatted. Section 11.4 is explicit that the GUI does
not silently reinterpret units, and the surest way not to reinterpret a number
is not to touch it: fractions stay fractions, and the percentage beside them
is derived for reading, never for storing.

The comparison exists because a version is immutable. Nobody edits a policy;
they write a new one and activate it, and the only way to see what that
changed is to put the two side by side.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.persistence.mysql.models.accounts import Account
from autotrader.persistence.mysql.models.risk import RiskPolicy, RiskPolicyVersion
from autotrader.persistence.mysql.repositories.policy_binding import (
    AccountPolicyBindings,
    PolicyBinding,
)
from autotrader.risk.v6 import (
    APPROVED_CAPITAL,
    MAX_LEVERAGE,
    SESSION_TRADE_UPPER_BOUND,
    TRADE_RISK_CEILING,
    ApprovedCapital,
)

# The fields that decide a trade's size, in the order an operator reads them.
SIZING_FIELDS = (
    "normal_risk_fraction",
    "a_candidate_risk_fraction",
    "a_risk_fraction",
    "absolute_trade_risk_fraction",
    "daily_loss_fraction",
    "weekly_loss_fraction",
    "max_open_structural_risk_fraction",
)
COUNT_FIELDS = ("max_consecutive_losses",)
FRESHNESS_FIELDS = (
    "account_age_seconds",
    "risk_age_seconds",
    "quote_age_seconds",
    "provider_age_seconds",
    "stream_gap_age_seconds",
    "completed_intraday_bar_arrival_seconds",
)


@dataclass(frozen=True, slots=True)
class PolicyFieldView:
    """One stored value, and its reading, kept apart."""

    name: str
    value: Decimal | int | bool | None
    percentage: str | None

    @property
    def display(self) -> str:
        if self.value is None:
            return "-"
        if self.percentage is not None:
            # Both, always. The fraction is what is stored and what the engine
            # uses; the percentage is only there to be read.
            return f"{self.value} ({self.percentage})"
        return str(self.value)


@dataclass(frozen=True, slots=True)
class PolicyVersionView:
    policy_code: str
    version_id: UUID
    version: str
    active: bool
    sizing: tuple[PolicyFieldView, ...]
    counts: tuple[PolicyFieldView, ...]
    freshness: tuple[PolicyFieldView, ...]

    def field(self, name: str) -> PolicyFieldView | None:
        for group in (self.sizing, self.counts, self.freshness):
            for field in group:
                if field.name == name:
                    return field
        return None


@dataclass(frozen=True, slots=True)
class PolicyDifference:
    """One field two versions disagree about."""

    name: str
    left: str
    right: str


@dataclass(frozen=True, slots=True)
class CapitalView:
    """One approved figure, and what kind of figure it is."""

    market: str
    amount: str
    unit: str
    kind: str


@dataclass(frozen=True, slots=True)
class PolicyCeilings:
    """Limits a policy cannot widen, shown so nobody looks for them above."""

    max_leverage: int
    session_trade_upper_bound: int
    trade_risk_fraction: Decimal
    trade_risk_percentage: str
    capital: tuple[CapitalView, ...]


@dataclass(frozen=True, slots=True)
class BindingView:
    """One account and the policy version it trades under."""

    account_id: UUID
    account_alias: str
    environment: str
    enabled: bool
    policy_code: str | None
    version: str | None
    scope: str | None

    @property
    def bound(self) -> bool:
        return self.version is not None


@dataclass(frozen=True, slots=True)
class PoliciesView:
    versions: tuple[PolicyVersionView, ...]
    ceilings: PolicyCeilings
    bindings: tuple[BindingView, ...]


def as_percentage(value: Decimal) -> str:
    """A reading, not a stored value.

    Trailing zeros are kept off so 0.0015 reads as 0.15% rather than
    0.1500%, which invites being mistaken for a different number.
    """
    return f"{(value * 100).normalize():f}%"


def approved_ceilings() -> PolicyCeilings:
    """The approved figures, read from the code that is their authority.

    Restating any of them here would create a second place to change them, and
    the screen exists to show what the engine will do, not what a template
    remembers it doing.
    """
    return PolicyCeilings(
        max_leverage=MAX_LEVERAGE,
        session_trade_upper_bound=SESSION_TRADE_UPPER_BOUND,
        trade_risk_fraction=TRADE_RISK_CEILING,
        trade_risk_percentage=as_percentage(TRADE_RISK_CEILING),
        capital=tuple(_capital(item) for item in APPROVED_CAPITAL),
    )


def _capital(item: ApprovedCapital) -> CapitalView:
    # Grouped, because 1000000 and 100000 are one glance apart and this is the
    # number an operator checks their account against.
    amount = f"{item.amount:,f}"
    if "." in amount:
        # Only the fraction may lose zeros. Trimming the whole string turns
        # 1,000,000 into 1,000.
        amount = amount.rstrip("0").rstrip(".")
    return CapitalView(
        market=item.market.value,
        amount=amount,
        unit=item.unit,
        kind=item.kind,
    )


def difference(
    left: PolicyVersionView, right: PolicyVersionView
) -> tuple[PolicyDifference, ...]:
    """Every field the two do not agree on.

    Fields present in one and absent in the other count as a difference: a
    cash policy has no A-candidate fraction and a futures policy does, and
    that is the sort of thing an operator most needs to see.
    """
    names = SIZING_FIELDS + COUNT_FIELDS + FRESHNESS_FIELDS
    found: list[PolicyDifference] = []
    for name in names:
        one, other = left.field(name), right.field(name)
        first = "-" if one is None else one.display
        second = "-" if other is None else other.display
        if first != second:
            found.append(PolicyDifference(name=name, left=first, right=second))
    return tuple(found)


class PoliciesReadModel:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load(self) -> PoliciesView:
        return PoliciesView(
            versions=await self.versions(),
            ceilings=approved_ceilings(),
            bindings=await self.bindings(),
        )

    async def bindings(self) -> tuple[BindingView, ...]:
        """Every account, bound or not.

        An account with no binding is the case worth seeing: it cannot trade,
        and a list that showed only bound accounts would hide it.
        """
        async with self._sessions() as session:
            accounts = (
                await session.scalars(select(Account).order_by(Account.account_alias))
            ).all()
            repository = AccountPolicyBindings(session)
            # Built inside the session. A rollback expires these instances and
            # a detached one raises on attribute access.
            views = [
                _binding_view(account, await repository.active_binding(account.id))
                for account in accounts
            ]
            await session.rollback()
        return tuple(views)

    async def versions(self) -> tuple[PolicyVersionView, ...]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(RiskPolicyVersion, RiskPolicy.code)
                    .join(RiskPolicy, RiskPolicy.id == RiskPolicyVersion.policy_id)
                    .order_by(RiskPolicy.code, RiskPolicyVersion.version)
                )
            ).all()
        return tuple(_view(row, code) for row, code in rows)


def _binding_view(account: Account, binding: PolicyBinding | None) -> BindingView:
    return BindingView(
        account_id=account.id,
        account_alias=account.account_alias,
        environment=account.environment,
        enabled=account.enabled,
        policy_code=None if binding is None else binding.policy_code,
        version=None if binding is None else binding.version,
        scope=None if binding is None else binding.scope,
    )


def _view(row: RiskPolicyVersion, code: str) -> PolicyVersionView:
    return PolicyVersionView(
        policy_code=code,
        version_id=row.id,
        version=row.version,
        active=row.active,
        sizing=tuple(_fraction(row, name) for name in SIZING_FIELDS),
        counts=tuple(_plain(row, name) for name in COUNT_FIELDS),
        freshness=tuple(_plain(row, name) for name in FRESHNESS_FIELDS),
    )


def _fraction(row: RiskPolicyVersion, name: str) -> PolicyFieldView:
    value = getattr(row, name)
    return PolicyFieldView(
        name=name,
        value=value,
        percentage=None if value is None else as_percentage(value),
    )


def _plain(row: RiskPolicyVersion, name: str) -> PolicyFieldView:
    return PolicyFieldView(name=name, value=getattr(row, name), percentage=None)


__all__ = (
    "COUNT_FIELDS",
    "FRESHNESS_FIELDS",
    "SIZING_FIELDS",
    "BindingView",
    "CapitalView",
    "PoliciesReadModel",
    "PoliciesView",
    "PolicyCeilings",
    "PolicyDifference",
    "PolicyFieldView",
    "PolicyVersionView",
    "approved_ceilings",
    "as_percentage",
    "difference",
)
