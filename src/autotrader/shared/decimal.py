from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BeforeValidator

from autotrader.shared.errors import DomainInvariantError, FloatRejectedError


def require_decimal(value: object) -> Decimal:
    if isinstance(value, float):
        raise FloatRejectedError("float values are not allowed at a domain boundary")
    if not isinstance(value, Decimal):
        raise TypeError("Decimal is required at a domain boundary")
    if not value.is_finite():
        raise DomainInvariantError("finite Decimal is required")
    return value


def require_non_negative(value: object) -> Decimal:
    value = require_decimal(value)
    if value < 0:
        raise DomainInvariantError("non-negative Decimal is required")
    return value


def decimal_to_string(value: Decimal) -> str:
    return format(require_decimal(value), "f")


def parse_contract_decimal(value: object) -> Decimal:
    if isinstance(value, float):
        raise ValueError("float values are not allowed at a domain boundary")
    if isinstance(value, str):
        try:
            value = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("valid Decimal string is required") from error
    return require_decimal(value)


def parse_non_negative_contract_decimal(value: object) -> Decimal:
    try:
        return require_non_negative(parse_contract_decimal(value))
    except FloatRejectedError as error:
        raise ValueError(str(error)) from error


type ContractDecimal = Annotated[Decimal, BeforeValidator(parse_contract_decimal)]
type NonNegativeDecimal = Annotated[
    Decimal, BeforeValidator(parse_non_negative_contract_decimal)
]
