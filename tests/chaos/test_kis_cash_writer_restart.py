from __future__ import annotations

import pytest

from autotrader.integrations.brokers.kis.cash_order_transport import (
    KisCashOrderTransport,
)
from autotrader.integrations.brokers.kis.cash_writer import KisCashWriter
from tests.integration.execution.test_kis_cash_writer import (
    Authority,
    Recovery,
    _command,
    _context,
)
from tests.unit.integrations.brokers.kis.test_kis_cash_order_transport import (
    MemoryDispatchStore,
    ScriptedSender,
    _success,
)


@pytest.mark.asyncio
async def test_restart_after_acknowledgement_does_not_submit_again() -> None:
    command = _command()
    context = _context(command)
    store = MemoryDispatchStore()
    first_sender = ScriptedSender(_success())
    first = KisCashWriter(
        authority=Authority(context),
        transport=KisCashOrderTransport(store, first_sender),
        recovery=Recovery(),
    )
    accepted = await first.submit_locked(command)

    restarted_sender = ScriptedSender(_success())
    restarted = KisCashWriter(
        authority=Authority(context),
        transport=KisCashOrderTransport(store, restarted_sender),
        recovery=Recovery(),
    )
    replayed = await restarted.submit_locked(command)

    assert replayed == accepted
    assert first_sender.calls == 1
    assert restarted_sender.calls == 0
