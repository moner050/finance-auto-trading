"""Reading the put-call ratio from the venue that has the options.

The ratio is one division, so the tests are mostly about what happens when the
inputs are not what they should be — because the failure that matters here is
a bad number arriving as a plausible one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from autotrader.integrations.market_data.deribit_put_call import (
    DeribitError,
    PutCallReading,
    read_put_call,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class _Deribit:
    def __init__(self, entries: tuple[dict[str, object], ...]) -> None:
        self._entries = entries

    async def trade_volumes(self) -> tuple[dict[str, object], ...]:
        return self._entries


def _entry(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "currency": "BTC",
        "calls_volume": 44592.0,
        "puts_volume": 14563.3,
        "futures_volume": 14865.49,
    }
    payload.update(changes)
    return payload


def _read(*entries: dict[str, object]) -> PutCallReading:
    return asyncio.run(read_put_call(_Deribit(entries), now=NOW))  # type: ignore[arg-type]


def test_the_ratio_is_puts_over_calls() -> None:
    found = _read(_entry())

    assert found.ratio == Decimal("14563.3") / Decimal("44592.0")


def test_the_currency_asked_for_is_the_one_read() -> None:
    """Deribit answers for every listed currency in one response."""
    found = _read(
        _entry(currency="ETH", calls_volume=1.0, puts_volume=9.0),
        _entry(),
    )

    assert found.currency == "BTC"
    assert found.calls_volume == Decimal("44592.0")


def test_a_currency_deribit_did_not_report_is_refused() -> None:
    with pytest.raises(DeribitError, match="no option volumes"):
        _read(_entry(currency="ETH"))


def test_a_day_with_no_call_volume_has_no_ratio() -> None:
    """Not an enormous one. A division by zero reported as a very pessimistic
    market is the measurement failing while looking like a signal."""
    found = _read(_entry(calls_volume=0.0))

    with pytest.raises(ValueError, match="no put-call ratio"):
        _ = found.ratio


def test_a_day_with_no_put_volume_is_a_ratio_of_zero() -> None:
    """Nobody bought puts is a real reading, unlike nobody bought calls."""
    found = _read(_entry(puts_volume=0.0))

    assert found.ratio == Decimal(0)


def test_a_volume_sent_as_text_is_refused() -> None:
    with pytest.raises(DeribitError, match="not sent as a number"):
        _read(_entry(calls_volume="44592.0"))


def test_a_boolean_is_not_a_volume() -> None:
    """`True` is an int in Python, and would otherwise become a volume of
    one."""
    with pytest.raises(DeribitError, match="not sent as a number"):
        _read(_entry(puts_volume=True))


def test_a_negative_volume_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _read(_entry(puts_volume=-1.0))


def test_the_reading_carries_the_moment_it_describes() -> None:
    """A percentile is built by ranking these against each other, so a reading
    that did not say when it was taken could not be placed in the series."""
    assert _read(_entry()).as_of == NOW


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PutCallReading(
            as_of=datetime(2026, 8, 28, 12, 0),
            currency="BTC",
            calls_volume=Decimal(1),
            puts_volume=Decimal(1),
        )
