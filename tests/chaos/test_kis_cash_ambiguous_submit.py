from __future__ import annotations

import pytest

from autotrader.integrations.brokers.kis.cash_order_transport import (
    KisCashOrderTransport,
    KisDispatchState,
    KisPostSendFailure,
    KisPostSendFailureKind,
)
from autotrader.shared.ids import new_uuid7
from tests.unit.integrations.brokers.kis.test_kis_cash_order_transport import (
    MemoryDispatchStore,
    ScriptedSender,
    _request,
    _success,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error_class"),
    (
        (
            KisPostSendFailure(KisPostSendFailureKind.TIMEOUT),
            "POST_SEND_TIMEOUT",
        ),
        (
            KisPostSendFailure(KisPostSendFailureKind.CONNECTION_RESET),
            "POST_SEND_RESET",
        ),
    ),
)
async def test_post_send_failures_are_ambiguous_and_never_reposted(
    failure: KisPostSendFailure, error_class: str
) -> None:
    dispatch_id = new_uuid7()
    store = MemoryDispatchStore()
    first_sender = ScriptedSender(failure)

    first = await KisCashOrderTransport(store, first_sender).dispatch_once(
        dispatch_id, _request()
    )

    restarted_sender = ScriptedSender(_success())
    restarted = await KisCashOrderTransport(store, restarted_sender).dispatch_once(
        dispatch_id, _request()
    )

    assert first.state is KisDispatchState.AMBIGUOUS
    assert first.error_class == error_class
    assert restarted == first
    assert first_sender.calls == 1
    assert restarted_sender.calls == 0


@pytest.mark.asyncio
async def test_process_restart_does_not_repeat_acknowledged_dispatch() -> None:
    dispatch_id = new_uuid7()
    store = MemoryDispatchStore()
    first = await KisCashOrderTransport(
        store, ScriptedSender(_success())
    ).dispatch_once(dispatch_id, _request())
    restarted_sender = ScriptedSender(_success())

    restarted = await KisCashOrderTransport(store, restarted_sender).dispatch_once(
        dispatch_id, _request()
    )

    assert first.state is KisDispatchState.ACKNOWLEDGED
    assert restarted == first
    assert restarted_sender.calls == 0
