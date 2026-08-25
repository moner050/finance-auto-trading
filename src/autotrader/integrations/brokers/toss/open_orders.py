from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, cast

from autotrader.integrations.brokers.common import BrokerResponse


class TossIncompleteOpenOrdersEvidence(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TossEmptyOpenOrdersEvidence:
    source_hash: bytes

    def __post_init__(self) -> None:
        if type(self.source_hash) is not bytes or self.source_hash != _empty_hash():
            raise ValueError("Toss empty OPEN orders evidence is invalid")


class _TossOpenOrdersReadPort(Protocol):
    def read_orders(
        self, *, access_token: str, account_seq: int, status: str
    ) -> Coroutine[object, object, BrokerResponse]: ...


async def read_empty_open_orders(
    *,
    adapter: object,
    access_token: object,
    account: object,
) -> TossEmptyOpenOrdersEvidence:
    outcome = await _read_outcome(
        adapter=adapter,
        access_token=access_token,
        account=account,
    )
    del adapter, access_token, account
    if isinstance(outcome, BaseException):
        error = outcome
        outcome = None
        _scrub_control(error)
        raise error from None
    if outcome is None:
        raise TossIncompleteOpenOrdersEvidence(
            "Toss OPEN orders evidence is incomplete"
        ) from None
    return outcome


async def _read_outcome(
    *,
    adapter: object,
    access_token: object,
    account: object,
) -> TossEmptyOpenOrdersEvidence | BaseException | None:
    response: object = None
    try:
        from autotrader.integrations.brokers.toss.adapter import TossAccount

        if (
            type(access_token) is not str
            or not access_token
            or "\n" in access_token
            or type(account) is not TossAccount
            or account.account_type != "BROKERAGE"
        ):
            raise ValueError("Toss OPEN orders input is invalid")
        account.__post_init__()
        reader = cast(_TossOpenOrdersReadPort, adapter)
        response = await reader.read_orders(
            access_token=access_token,
            account_seq=account.account_seq,
            status="OPEN",
        )
        return decode_toss_empty_open_orders(response)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
        _scrub_control(caught)
        return caught
    except Exception as caught:
        _scrub_exception(caught)
        return None
    finally:
        response = None
        del adapter, access_token, account


def decode_toss_empty_open_orders(
    response: BrokerResponse,
) -> TossEmptyOpenOrdersEvidence:
    if type(response) is not BrokerResponse:
        raise ValueError("Toss OPEN orders response is invalid")
    status = response.status
    body = response.body
    del response
    try:
        if status != 200:
            raise ValueError("Toss OPEN orders response is invalid")
        try:
            payload: object = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError("Toss OPEN orders response is invalid") from error
        if not isinstance(payload, Mapping):
            raise ValueError("Toss OPEN orders response is invalid")
        exact_payload = cast(Mapping[object, object], payload)
        if set(exact_payload) != {"result"}:
            raise ValueError("Toss OPEN orders response is invalid")
        result = exact_payload["result"]
        if not isinstance(result, Mapping):
            raise ValueError("Toss OPEN orders response is invalid")
        exact = cast(Mapping[str, object], result)
        if (
            set(exact) != {"orders", "nextCursor", "hasNext"}
            or type(exact["orders"]) is not list
            or exact["orders"] != []
            or exact["nextCursor"] is not None
            or exact["hasNext"] is not False
        ):
            raise ValueError("Toss OPEN orders response is invalid")
        return TossEmptyOpenOrdersEvidence(source_hash=_empty_hash())
    finally:
        del body


def _empty_hash() -> bytes:
    digest = sha256()
    for value in (
        b"TOSS_EMPTY_OPEN_ORDERS_EVIDENCE_V1",
        b"GET",
        b"/api/v1/orders?status=OPEN",
        b"orders=[]",
        b"nextCursor=null",
        b"hasNext=false",
    ):
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return digest.digest()


def _scrub_exception(caught: BaseException) -> None:
    caught.__traceback__ = None
    caught.__context__ = None
    caught.__cause__ = None
    caught.args = ()
    caught.__dict__.clear()


def _scrub_control(caught: BaseException) -> None:
    _scrub_exception(caught)
    if isinstance(caught, SystemExit):
        caught.code = 1
