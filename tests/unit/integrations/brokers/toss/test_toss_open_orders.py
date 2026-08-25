from __future__ import annotations

import asyncio
import json
import reprlib
from collections.abc import Iterator
from types import FrameType, TracebackType
from typing import cast

import pytest

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.toss.adapter import (
    TossAccount,
    TossReadOnlyAdapter,
)
from autotrader.integrations.brokers.toss.open_orders import (
    TossEmptyOpenOrdersEvidence,
    TossIncompleteOpenOrdersEvidence,
    read_empty_open_orders,
)


def _response(
    *,
    status: int = 200,
    orders: object = None,
    next_cursor: object = None,
    has_next: object = False,
) -> BrokerResponse:
    return BrokerResponse(
        status=status,
        body=json.dumps(
            {
                "result": {
                    "orders": [] if orders is None else orders,
                    "nextCursor": next_cursor,
                    "hasNext": has_next,
                }
            },
            separators=(",", ":"),
        ).encode(),
    )


class _Reader:
    def __init__(self, response: BrokerResponse | BaseException) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def read_orders(self, **kwargs: object) -> BrokerResponse:
        self.calls.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _Transport:
    def __init__(self, response: BrokerResponse | None = None) -> None:
        self.response = _response() if response is None else response
        self.requests: list[BrokerRequest] = []

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.response


@pytest.mark.asyncio
async def test_reads_one_canonical_empty_open_order_projection() -> None:
    reader = _Reader(_response())
    account = TossAccount(account_seq=17, account_type="BROKERAGE")

    evidence = await read_empty_open_orders(
        adapter=reader,
        access_token="private-token",
        account=account,
    )

    assert evidence == TossEmptyOpenOrdersEvidence(source_hash=evidence.source_hash)
    assert len(evidence.source_hash) == 32
    assert reader.calls == [
        {
            "access_token": "private-token",
            "account_seq": 17,
            "status": "OPEN",
        }
    ]


@pytest.mark.asyncio
async def test_adapter_builds_only_the_exact_open_orders_request() -> None:
    transport = _Transport()
    adapter = TossReadOnlyAdapter(transport=transport)

    await read_empty_open_orders(
        adapter=adapter,
        access_token="private-token",
        account=TossAccount(account_seq=17, account_type="BROKERAGE"),
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/orders?status=OPEN",
            headers=(
                ("Authorization", "Bearer private-token"),
                ("X-Tossinvest-Account", "17"),
            ),
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        _response(status=500),
        _response(orders=[{"orderId": "private-order"}]),
        _response(next_cursor="private-cursor"),
        _response(has_next=True),
        BrokerResponse(200, b"not-json"),
        BrokerResponse(200, b'{"result":{"orders":[]}}'),
        BrokerResponse(
            200,
            b'{"result":{"orders":[],"nextCursor":null,"hasNext":false,"extra":true}}',
        ),
    ),
)
async def test_rejects_every_incomplete_or_nonempty_projection(
    response: BrokerResponse,
) -> None:
    with pytest.raises(
        TossIncompleteOpenOrdersEvidence,
        match="Toss OPEN orders evidence is incomplete",
    ) as raised:
        await read_empty_open_orders(
            adapter=_Reader(response),
            access_token="private-token",
            account=TossAccount(account_seq=17, account_type="BROKERAGE"),
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "account"),
    (
        ("", TossAccount(account_seq=17, account_type="BROKERAGE")),
        ("private\ntoken", TossAccount(account_seq=17, account_type="BROKERAGE")),
        ("private-token", object()),
    ),
)
async def test_invalid_input_fails_before_provider_read(
    token: str, account: object
) -> None:
    reader = _Reader(_response())

    with pytest.raises(TossIncompleteOpenOrdersEvidence):
        await read_empty_open_orders(
            adapter=reader,
            access_token=token,
            account=account,
        )

    assert reader.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control",
    (
        asyncio.CancelledError("private"),
        KeyboardInterrupt("private"),
        SystemExit("private"),
    ),
)
async def test_control_failure_is_sanitized_and_preserves_identity(
    control: BaseException,
) -> None:
    with pytest.raises(type(control)) as raised:
        await read_empty_open_orders(
            adapter=_Reader(control),
            access_token="private-token",
            account=TossAccount(account_seq=17, account_type="BROKERAGE"),
        )

    assert raised.value is control
    assert control.args == ()
    assert control.__cause__ is None
    assert control.__context__ is None
    if isinstance(control, SystemExit):
        assert control.code == 1


def test_evidence_constructor_recomputes_the_domain_hash() -> None:
    with pytest.raises(ValueError, match="evidence"):
        TossEmptyOpenOrdersEvidence(source_hash=b"x" * 32)
    with pytest.raises(ValueError, match="evidence"):
        TossEmptyOpenOrdersEvidence(source_hash=b"x" * 31)


@pytest.mark.asyncio
async def test_read_only_adapter_writes_remain_disabled() -> None:
    transport = _Transport()
    adapter = TossReadOnlyAdapter(transport=transport)

    with pytest.raises(BrokerWriteDisabled):
        await adapter.submit(command=object())
    with pytest.raises(BrokerWriteDisabled):
        await adapter.cancel(command=object())
    with pytest.raises(BrokerWriteDisabled):
        await adapter.replace(command=object())

    assert transport.requests == []


@pytest.mark.asyncio
async def test_public_failure_graph_retains_no_token_account_request_or_response() -> (
    None
):
    response = BrokerResponse(500, b'{"private":"raw-sentinel"}')
    reader = _Reader(response)
    account = TossAccount(account_seq=987654321, account_type="BROKERAGE")
    captured: TossIncompleteOpenOrdersEvidence | None = None
    forbidden_ids = frozenset({id(response), id(reader), id(account)})
    try:
        await read_empty_open_orders(
            adapter=reader,
            access_token="token-sentinel",
            account=account,
        )
    except TossIncompleteOpenOrdersEvidence as error:
        captured = error
    finally:
        response = None  # type: ignore[assignment]
        reader = None  # type: ignore[assignment]
        account = None  # type: ignore[assignment]
    assert captured is not None

    reachable = tuple(_reachable(captured))
    assert forbidden_ids.isdisjoint(id(value) for value in reachable)
    for sentinel in ("token-sentinel", "raw-sentinel", "987654321"):
        assert not any(_contains(value, sentinel) for value in reachable)


def _reachable(error: BaseException) -> Iterator[object]:
    pending: list[object] = [error]
    seen: set[int] = set()
    while pending and len(seen) < 500:
        value = pending.pop()
        if value is None or id(value) in seen:
            continue
        seen.add(id(value))
        yield value
        if isinstance(value, BaseException):
            pending.extend((value.__traceback__, value.__cause__, value.__context__))
            pending.extend(value.args)
        elif isinstance(value, TracebackType):
            pending.extend((value.tb_frame, value.tb_next))
        elif isinstance(value, FrameType):
            pending.extend(value.f_locals.values())
            pending.append(value.f_back)
        elif isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(cast(tuple[object, ...], value))
        elif isinstance(value, dict):
            pending.extend(cast(dict[object, object], value).items())
        elif hasattr(value, "__dict__"):
            pending.extend(cast(dict[str, object], value.__dict__).values())


def _contains(value: object, sentinel: str) -> bool:
    if isinstance(value, str):
        return sentinel in value
    if isinstance(value, bytes):
        return sentinel.encode() in value
    renderer = reprlib.Repr()
    renderer.maxother = 512
    renderer.maxstring = 512
    try:
        return sentinel in renderer.repr(value)
    except Exception:
        return False
