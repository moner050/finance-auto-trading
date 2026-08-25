from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    DecimalException,
    Inexact,
    Rounded,
    localcontext,
)

_QUANTUM = Decimal("0.000000000000000001")
_MAXIMUM = Decimal("99999999999999999999.999999999999999999")


@contextmanager
def mysql_numeric_localcontext() -> Generator[Context]:
    context = Context(prec=80, rounding=ROUND_HALF_UP)
    context.traps[Inexact] = False
    context.traps[Rounded] = False
    with localcontext(context) as active:
        yield active


def canonical_mysql_numeric_38_18(value: object, name: str) -> Decimal:
    """Return the context-independent MySQL NUMERIC(38,18) persisted value."""
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{name} must fit MySQL NUMERIC(38,18)")
    try:
        with mysql_numeric_localcontext():
            rounded = value.quantize(_QUANTUM)
    except DecimalException:
        raise ValueError(f"{name} must fit MySQL NUMERIC(38,18)") from None
    if rounded.copy_abs() > _MAXIMUM:
        raise ValueError(f"{name} must fit MySQL NUMERIC(38,18)")
    if rounded.is_zero():
        return Decimal(0)
    return rounded
