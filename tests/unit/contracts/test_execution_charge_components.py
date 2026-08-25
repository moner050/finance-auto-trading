from __future__ import annotations

from decimal import Decimal

import pytest

from autotrader.execution.fills.models import (
    ChargeBasis,
    ChargeEffect,
    ChargeLegRole,
    ExecutionChargeComponent,
)


def test_per_unit_charge_requires_only_positive_quantity_basis() -> None:
    charge = ExecutionChargeComponent(
        component_ordinal=0,
        amount=Decimal("1.25"),
        currency="USD",
        charge_kind="BROKER_COMMISSION",
        effect=ChargeEffect.DEBIT,
        leg_role=ChargeLegRole.ENTRY,
        charge_basis=ChargeBasis.PER_UNIT,
        basis_quantity=Decimal("2"),
        basis_notional=None,
    )

    assert charge.amount == Decimal("1.25")


@pytest.mark.parametrize(
    ("basis", "quantity", "notional"),
    [
        (ChargeBasis.PER_UNIT, None, None),
        (ChargeBasis.PER_UNIT, Decimal("1"), Decimal("1")),
        (ChargeBasis.PER_NOTIONAL, Decimal("1"), Decimal("1")),
        (ChargeBasis.PER_ORDER_MINIMUM, Decimal("1"), None),
    ],
)
def test_charge_basis_rejects_ambiguous_or_missing_basis(
    basis: ChargeBasis, quantity: Decimal | None, notional: Decimal | None
) -> None:
    with pytest.raises(ValueError):
        ExecutionChargeComponent(
            component_ordinal=0,
            amount=Decimal("1"),
            currency="USD",
            charge_kind="FEE",
            effect=ChargeEffect.DEBIT,
            leg_role=ChargeLegRole.EXIT_OTHER,
            charge_basis=basis,
            basis_quantity=quantity,
            basis_notional=notional,
        )
