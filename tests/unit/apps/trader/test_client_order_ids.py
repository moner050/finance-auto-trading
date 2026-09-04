"""Every client order id the loop mints has to be one the venue accepts.

Binance allows 36 characters. A UUID's hex is 32, so a prefix has four to
spend, and `stop-` and `exit-` were spending five. The stop never noticed - a
protective stop reaches Binance as an algo order under a different id - but an
exit is an ordinary market order, so every close this loop placed would have
been refused by the adapter before a request was built.

Nothing connected the two ends. The loop chose the prefixes and the adapter
validated the result, and neither knew about the other. This is that
connection, which is why it is a test rather than a comment.
"""

from __future__ import annotations

from uuid import uuid7

import pytest

from autotrader.apps.trader.composition import (
    ADD_PREFIX,
    ENTRY_PREFIX,
    EXIT_PREFIX,
    STOP_PREFIX,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    _client_order_id,
    binance_normal_client_order_id,
)

PREFIXES = (ENTRY_PREFIX, STOP_PREFIX, ADD_PREFIX, EXIT_PREFIX)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_every_prefix_yields_an_id_the_adapter_accepts(prefix: str) -> None:
    for _ in range(64):
        _client_order_id(f"{prefix}{uuid7().hex}")


def test_the_prefixes_are_distinct() -> None:
    """So an id says which leg it belongs to without a lookup."""
    assert len(set(PREFIXES)) == len(PREFIXES)
    for one in PREFIXES:
        assert not any(other.startswith(one) for other in PREFIXES if other != one)


def test_the_adapters_own_id_still_fits() -> None:
    """The emergency close is required to use exactly this one."""
    _client_order_id(binance_normal_client_order_id(uuid7()))
