from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest

from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse
from autotrader.integrations.brokers.kis.cash_order_transport import (
    KisCashOrderTransport,
    KisDispatchClaim,
    KisDispatchRecord,
    KisDispatchState,
    KisPreSendFailure,
)
from autotrader.shared.ids import new_uuid7


def _request(*, authorization: str = "Bearer secret-token") -> BrokerRequest:
    return BrokerRequest(
        method="POST",
        path="/uapi/domestic-stock/v1/trading/order-cash",
        headers=(("authorization", authorization), ("tr_id", "VTTC0802U")),
        body=b'{"ACNT_PRDT_CD":"01","CANO":"12345678","PDNO":"005930"}',
    )


def _success() -> BrokerResponse:
    return BrokerResponse(
        status=200,
        body=json.dumps(
            {
                "rt_cd": "0",
                "msg_cd": "APBK0013",
                "msg1": "accepted",
                "output": {
                    "KRX_FWDG_ORD_ORGNO": "12345",
                    "ODNO": "0000000042",
                    "ORD_TMD": "091501",
                },
            }
        ).encode(),
    )


class MemoryDispatchStore:
    def __init__(self) -> None:
        self.rows: dict[UUID, KisDispatchRecord] = {}
        self.persisted_values: list[object] = []

    async def prepare(
        self, dispatch_id: UUID, request_digest: bytes
    ) -> KisDispatchClaim:
        self.persisted_values.extend((dispatch_id, request_digest))
        current = self.rows.get(dispatch_id)
        if current is None:
            current = KisDispatchRecord.prepared(dispatch_id, request_digest)
            self.rows[dispatch_id] = current
            return KisDispatchClaim(record=current, acquired=True)
        if current.request_digest != request_digest:
            raise ValueError("dispatch request digest mismatch")
        if current.state is not KisDispatchState.NOT_SENT:
            return KisDispatchClaim(record=current, acquired=False)
        claimed = replace(
            current,
            state=KisDispatchState.PREPARED,
            fencing_token=current.fencing_token + 1,
            attempt_count=current.attempt_count + 1,
            error_class=None,
        )
        self.rows[dispatch_id] = claimed
        return KisDispatchClaim(record=claimed, acquired=True)

    async def finish(
        self,
        dispatch_id: UUID,
        *,
        fencing_token: int,
        state: KisDispatchState,
        response_digest: bytes | None,
        error_class: str | None,
        organization_number: str | None,
        order_number: str | None,
        order_time: str | None,
        message_code: str | None,
    ) -> KisDispatchRecord:
        self.persisted_values.extend(
            (
                dispatch_id,
                fencing_token,
                state,
                response_digest,
                error_class,
                organization_number,
                order_number,
                order_time,
                message_code,
            )
        )
        current = self.rows[dispatch_id]
        if (
            current.state is not KisDispatchState.PREPARED
            or current.fencing_token != fencing_token
        ):
            raise RuntimeError("stale KIS dispatch fencing token")
        completed = replace(
            current,
            state=state,
            response_digest=response_digest,
            error_class=error_class,
            organization_number=organization_number,
            order_number=order_number,
            order_time=order_time,
            message_code=message_code,
        )
        completed.validate()
        self.rows[dispatch_id] = completed
        return completed


class ScriptedSender:
    def __init__(self, *outcomes: BrokerResponse | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        del request
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_pre_send_connect_failure_is_the_only_retryable_state() -> None:
    dispatch_id = new_uuid7()
    store = MemoryDispatchStore()
    failed_sender = ScriptedSender(KisPreSendFailure())

    first = await KisCashOrderTransport(store, failed_sender).dispatch_once(
        dispatch_id, _request()
    )

    assert first.state is KisDispatchState.NOT_SENT
    assert first.error_class == "PRE_SEND_CONNECT_FAILURE"
    assert first.attempt_count == 1

    retry_sender = ScriptedSender(_success())
    second = await KisCashOrderTransport(store, retry_sender).dispatch_once(
        dispatch_id, _request()
    )

    assert second.state is KisDispatchState.ACKNOWLEDGED
    assert second.attempt_count == 2
    assert second.order_number == "0000000042"
    assert retry_sender.calls == 1


@pytest.mark.asyncio
async def test_http_business_failure_is_durably_rejected() -> None:
    body = {
        "rt_cd": "1",
        "msg_cd": "APBK0919",
        "msg1": "account 12345678 rejected",
        "output": {},
    }
    sender = ScriptedSender(BrokerResponse(status=400, body=json.dumps(body).encode()))
    store = MemoryDispatchStore()

    result = await KisCashOrderTransport(store, sender).dispatch_once(
        new_uuid7(), _request()
    )

    assert result.state is KisDispatchState.REJECTED
    assert result.error_class == "PROVIDER_BUSINESS_REJECTION"
    assert result.response_digest is not None
    assert "12345678" not in repr(store.persisted_values)
    assert "secret-token" not in repr(store.persisted_values)


@pytest.mark.asyncio
async def test_duplicate_worker_is_fenced_while_first_dispatch_is_prepared() -> None:
    dispatch_id = new_uuid7()
    store = MemoryDispatchStore()
    request = _request()
    first_claim = await store.prepare(
        dispatch_id,
        KisCashOrderTransport.request_digest(request),
    )
    assert first_claim.acquired
    sender = ScriptedSender(_success())

    result = await KisCashOrderTransport(store, sender).dispatch_once(
        dispatch_id, request
    )

    assert result.state is KisDispatchState.PREPARED
    assert sender.calls == 0


@pytest.mark.asyncio
async def test_changed_request_cannot_reuse_a_dispatch_identity() -> None:
    dispatch_id = new_uuid7()
    store = MemoryDispatchStore()
    await KisCashOrderTransport(store, ScriptedSender(_success())).dispatch_once(
        dispatch_id, _request()
    )

    with pytest.raises(ValueError, match="digest mismatch"):
        await KisCashOrderTransport(store, ScriptedSender(_success())).dispatch_once(
            dispatch_id,
            BrokerRequest(
                method="POST",
                path="/uapi/domestic-stock/v1/trading/order-cash",
                headers=(
                    ("authorization", "Bearer changed"),
                    ("tr_id", "VTTC0802U"),
                ),
                body=b'{"ACNT_PRDT_CD":"01","CANO":"12345678","PDNO":"000660"}',
            ),
        )


@pytest.mark.asyncio
async def test_rotated_authorization_secret_does_not_change_dispatch_identity() -> None:
    dispatch_id = new_uuid7()
    store = MemoryDispatchStore()
    first = await KisCashOrderTransport(
        store, ScriptedSender(_success())
    ).dispatch_once(dispatch_id, _request())
    restarted_sender = ScriptedSender(_success())

    rotated = await KisCashOrderTransport(store, restarted_sender).dispatch_once(
        dispatch_id,
        _request(authorization="Bearer rotated-secret"),
    )

    assert rotated == first
    assert restarted_sender.calls == 0
    assert "rotated-secret" not in repr(store.persisted_values)


@pytest.mark.asyncio
async def test_unknown_sender_failure_is_ambiguous_not_retryable() -> None:
    sender = ScriptedSender(RuntimeError("unexpected socket failure"))
    result = await KisCashOrderTransport(MemoryDispatchStore(), sender).dispatch_once(
        new_uuid7(), _request()
    )

    assert result.state is KisDispatchState.AMBIGUOUS
    assert result.error_class == "UNCLASSIFIED_POST_PREPARE_FAILURE"


__all__ = ("MemoryDispatchStore", "ScriptedSender", "_request", "_success")
