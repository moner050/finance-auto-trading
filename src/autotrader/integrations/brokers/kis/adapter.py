from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import NoReturn, cast
from urllib.parse import urlencode
from uuid import UUID

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
    UnsupportedBrokerInstrument,
)
from autotrader.integrations.brokers.kis.cash_writer import (
    BrokerWriteResult,
    KisCashWriter,
)
from autotrader.integrations.brokers.kis.contracts import (
    KisActiveContract,
    KisContractMasterReader,
)
from autotrader.integrations.brokers.kis.oauth import (
    KisAccessToken,
    KisClientCredentials,
    issue_kis_access_token,
)
from autotrader.integrations.brokers.kis.oauth import (
    KisAuthenticationError as _KisAuthenticationError,
)
from autotrader.integrations.brokers.kis.read_contracts import KisReadCredentials
from autotrader.shared.decimal import (
    decimal_to_string,
    parse_contract_decimal,
    require_decimal,
)

KisAuthenticationError = _KisAuthenticationError


class KisIncompleteAccountSnapshot(RuntimeError):
    """Raised when KIS requires another page before a snapshot is complete."""


class KisIncompleteContractSnapshot(RuntimeError):
    """Raised when a KIS contract-detail observation is incomplete."""


class KisIncompleteMinuteChartSnapshot(RuntimeError):
    """Raised when a KIS minute-chart observation cannot be collected completely."""


class KisMinuteChartInterval(StrEnum):
    ONE_MINUTE = "1"
    FIVE_MINUTES = "5"
    TEN_MINUTES = "10"
    FIFTEEN_MINUTES = "15"
    THIRTY_MINUTES = "30"
    SIXTY_MINUTES = "60"


@dataclass(frozen=True, slots=True)
class KisMinuteChartRecord:
    """A provider-local KIS minute record; it is not a completed OHLCV bar."""

    trading_date: date
    trading_time: time
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    cumulative_volume: Decimal

    def __post_init__(self) -> None:
        if type(self.trading_date) is not date or type(self.trading_time) is not time:
            raise ValueError("KIS minute record requires a local date and time")
        for name in (
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "cumulative_volume",
        ):
            value = require_decimal(getattr(self, name))
            if value < 0 or (name != "cumulative_volume" and value == 0):
                raise ValueError("KIS minute record prices and volume are invalid")
            object.__setattr__(self, name, value)
        if self.high_price < self.low_price or not (
            self.low_price <= self.open_price <= self.high_price
            and self.low_price <= self.close_price <= self.high_price
        ):
            raise ValueError("KIS minute record price range is invalid")


@dataclass(frozen=True, slots=True)
class KisMinuteChartPage:
    """One decoded KIS response page, without timestamp or volume inference."""

    records: tuple[KisMinuteChartRecord, ...]
    continuation_key: str | None

    def __post_init__(self) -> None:
        records = cast(object, self.records)
        if not isinstance(records, tuple):
            raise ValueError("KIS minute chart records must be an immutable tuple")
        records = cast(tuple[object, ...], records)
        if not all(isinstance(record, KisMinuteChartRecord) for record in records):
            raise ValueError("KIS minute chart records must be an immutable tuple")
        if self.continuation_key is not None:
            _continuation_key(self.continuation_key)


@dataclass(frozen=True, slots=True)
class KisFuturesPriceRecord:
    """A provider-local KIS futures quote; it is not a completed strategy bar."""

    provider_date: date
    provider_time: time
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    last_price: Decimal
    cumulative_volume: Decimal
    exchange_code: str
    currency_code: str

    def __post_init__(self) -> None:
        if type(self.provider_date) is not date or type(self.provider_time) is not time:
            raise ValueError(
                "KIS futures price requires a provider local date and time"
            )
        for name in (
            "open_price",
            "high_price",
            "low_price",
            "last_price",
            "cumulative_volume",
        ):
            value = require_decimal(getattr(self, name))
            if value < 0 or (name != "cumulative_volume" and value == 0):
                raise ValueError("KIS futures price fields are invalid")
            object.__setattr__(self, name, value)
        if self.high_price < self.low_price or not (
            self.low_price <= self.open_price <= self.high_price
            and self.low_price <= self.last_price <= self.high_price
        ):
            raise ValueError("KIS futures price range is invalid")
        for name in ("exchange_code", "currency_code"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or "\n" in value:
                raise ValueError("KIS futures price codes are invalid")


@dataclass(frozen=True, slots=True)
class KisFuturesPricePage:
    """One decoded KIS futures quote response without bar-completion inference."""

    records: tuple[KisFuturesPriceRecord, ...]

    def __post_init__(self) -> None:
        records = cast(object, self.records)
        if not isinstance(records, tuple):
            raise ValueError("KIS futures price records must be an immutable tuple")
        records = cast(tuple[object, ...], records)
        if not all(isinstance(record, KisFuturesPriceRecord) for record in records):
            raise ValueError("KIS futures price records must be an immutable tuple")


@dataclass(frozen=True, slots=True, init=False)
class KisContractDetailPage:
    """Raw KIS contract-detail evidence without contract-master authority."""

    provider_contract_codes: tuple[str, ...]
    canonical_payload_hash: bytes

    def __init__(
        self,
        *,
        provider_contract_codes: tuple[str, ...],
        canonical_payload_hash: bytes,
    ) -> NoReturn:
        del provider_contract_codes, canonical_payload_hash
        raise TypeError("KIS contract detail pages must be created by the decoder")

    @classmethod
    def from_provider_body(cls, *, body: bytes) -> KisContractDetailPage:
        try:
            payload: object = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(
                "KIS contract detail response is not valid JSON"
            ) from error
        if not isinstance(payload, Mapping):
            raise ValueError("KIS contract detail response is not an object")
        payload = cast(Mapping[str, object], payload)
        records = payload.get("output2")
        if payload.get("rt_cd") != "0" or not isinstance(records, list):
            raise ValueError("KIS contract detail response is incomplete")
        records = cast(list[object], records)
        return cls._from_provider_fields(
            body=body,
            provider_contract_codes=tuple(
                _contract_detail_code(record) for record in records
            ),
        )

    @classmethod
    def _from_provider_fields(
        cls, *, body: bytes, provider_contract_codes: tuple[str, ...]
    ) -> KisContractDetailPage:
        page = object.__new__(cls)
        object.__setattr__(page, "provider_contract_codes", provider_contract_codes)
        object.__setattr__(
            page, "canonical_payload_hash", hashlib.sha256(body).digest()
        )
        page._validate()
        return page

    def _validate(self) -> None:
        codes = _provider_contract_codes(self.provider_contract_codes)
        if len(set(codes)) != len(codes):
            raise ValueError("KIS contract detail codes must not repeat")
        payload_hash = cast(object, self.canonical_payload_hash)
        if not isinstance(payload_hash, bytes) or len(payload_hash) != 32:
            raise ValueError("KIS contract detail payload hash must be SHA-256")

    def require_provider_contract_code(self, value: object) -> str:
        code = _provider_contract_codes((value,))[0]
        if code not in self.provider_contract_codes:
            raise ValueError("KIS contract detail provider code is not present")
        return code


@dataclass(frozen=True, slots=True)
class KisFuturesOrderPreview:
    """A non-transmitting KIS overseas-futures order payload."""

    tr_id: str
    body: bytes

    def __post_init__(self) -> None:
        if self.tr_id != "OTFM3001U" or not self.body:
            raise ValueError("KIS futures order preview is invalid")


@dataclass(frozen=True, slots=True)
class KisFuturesOrderAcknowledgement:
    """The provider-local identifiers returned by an accepted KIS order."""

    local_order_date: str
    order_number: str

    def __post_init__(self) -> None:
        try:
            parsed_date = datetime.strptime(self.local_order_date, "%Y%m%d")
        except ValueError as error:
            raise ValueError("KIS acknowledged order date is invalid") from error
        if parsed_date.strftime("%Y%m%d") != self.local_order_date:
            raise ValueError("KIS acknowledged order date is invalid")
        if (
            len(self.order_number) != 8
            or not self.order_number.isascii()
            or not self.order_number.isdecimal()
        ):
            raise ValueError("KIS acknowledged order number is invalid")


@dataclass(frozen=True, slots=True)
class KisAccountReadCredentials:
    """Call-scoped credentials for KIS overseas-futures account observations."""

    access_token: str
    app_key: str
    app_secret: str
    account_number: str
    product_code: str

    def __post_init__(self) -> None:
        for name in ("access_token", "app_key", "app_secret"):
            value = getattr(self, name)
            if not value or "\n" in value:
                raise ValueError(f"{name} must be a non-empty single line")
        if not self.account_number.isascii() or not self.account_number.isdecimal():
            raise ValueError("KIS account number must contain eight digits")
        if len(self.account_number) != 8:
            raise ValueError("KIS account number must contain eight digits")
        if self.product_code != "08":
            raise ValueError("KIS overseas futures product code must be 08")


def build_kis_futures_order_preview(
    *,
    command: BrokerOrderCommand,
    account: KisAccountReadCredentials,
    contract: KisActiveContract,
    now: datetime,
) -> KisFuturesOrderPreview:
    """Builds the documented KIS payload without creating a network request."""
    if command.command_type is not CommandType.SUBMIT:
        raise ValueError("KIS futures preview requires a submit command")
    if command.target_broker_order_id is not None:
        raise ValueError("KIS futures submit command cannot target an existing order")
    if now.tzinfo is not UTC or now.utcoffset() != UTC.utcoffset(now):
        raise ValueError("KIS futures preview requires UTC now")
    if (
        command.not_after.tzinfo is not UTC
        or command.not_after.utcoffset() != UTC.utcoffset(command.not_after)
    ):
        raise ValueError("KIS futures preview command not_after must be UTC")
    if now >= command.not_after:
        raise ValueError("KIS futures preview command not_after is expired")
    if contract.instrument_id != command.instrument_id:
        raise ValueError("KIS contract evidence does not match command instrument")
    if now >= contract.expires_at:
        raise ValueError("KIS contract evidence is expired")
    if command.time_in_force != "DAY":
        raise ValueError("KIS futures preview supports DAY only")
    quantity = require_decimal(command.quantity)
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise ValueError("KIS futures preview requires a positive whole quantity")
    side = _kis_order_side(command.side)
    price_division, limit_price, condition = _kis_order_terms(command)
    return KisFuturesOrderPreview(
        tr_id="OTFM3001U",
        body=json.dumps(
            {
                "CANO": account.account_number,
                "ACNT_PRDT_CD": account.product_code,
                "OVRS_FUTR_FX_PDNO": contract.provider_contract_code,
                "SLL_BUY_DVSN_CD": side,
                "FM_LQD_USTL_CCLD_DT": "",
                "FM_LQD_USTL_CCNO": "",
                "PRIC_DVSN_CD": price_division,
                "FM_LIMIT_ORD_PRIC": limit_price,
                "FM_STOP_ORD_PRIC": "",
                "FM_ORD_QTY": decimal_to_string(quantity),
                "FM_LQD_LMT_ORD_PRIC": "",
                "FM_LQD_STOP_ORD_PRIC": "",
                "CCLD_CNDT_CD": condition,
                "CPLX_ORD_DVSN_CD": "0",
                "ECIS_RSVN_ORD_YN": "N",
                "FM_HDGE_ORD_SCRN_YN": "N",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def decode_kis_futures_order_acknowledgement(
    response: BrokerResponse,
) -> KisFuturesOrderAcknowledgement:
    """Decodes the documented KIS overseas-futures order identifiers."""
    if response.status != 200:
        raise ValueError("KIS order acknowledgement is not successful")
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("KIS order acknowledgement is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("KIS order acknowledgement is not an object")
    payload = cast(Mapping[str, object], payload)
    output = payload.get("output")
    if payload.get("rt_cd") != "0" or not isinstance(output, Mapping):
        raise ValueError("KIS order acknowledgement is incomplete")
    output = cast(Mapping[str, object], output)
    order_date = output.get("ORD_DT")
    order_number = output.get("ODNO")
    if not isinstance(order_date, str) or not isinstance(order_number, str):
        raise ValueError("KIS order acknowledgement is incomplete")
    return KisFuturesOrderAcknowledgement(
        local_order_date=order_date,
        order_number=order_number,
    )


class KisReadOnlyAdapter:
    """Sends a KIS quote request only with exact active master evidence."""

    base_url = "https://openapi.koreainvestment.com:9443"
    _price_path = "/uapi/overseas-futureoption/v1/quotations/inquire-price"
    _price_tr_id = "HHDFC55010000"
    _minute_chart_path = (
        "/uapi/overseas-futureoption/v1/quotations/inquire-time-futurechartprice"
    )
    _minute_chart_tr_id = "HHDFC55020400"
    _contract_detail_path = (
        "/uapi/overseas-futureoption/v1/quotations/search-contract-detail"
    )
    _contract_detail_tr_id = "HHDFC55200000"
    _open_position_path = "/uapi/overseas-futureoption/v1/trading/inquire-unpd"
    _open_position_tr_id = "OTFM1412R"
    _daily_order_path = "/uapi/overseas-futureoption/v1/trading/inquire-daily-order"
    _daily_order_tr_id = "OTFM3120R"

    def __init__(
        self,
        *,
        transport: AsyncHttpTransport,
        contract_reader: KisContractMasterReader,
    ) -> None:
        self._transport = transport
        self._contract_reader = contract_reader

    async def issue_access_token(
        self, *, credentials: KisClientCredentials
    ) -> KisAccessToken:
        return await issue_kis_access_token(
            transport=self._transport,
            credentials=credentials,
        )

    async def read_complete_contract_details(
        self,
        *,
        credentials: KisReadCredentials,
        provider_contract_codes: object,
        max_pages: object,
    ) -> tuple[BrokerResponse, ...]:
        contract_codes = _provider_contract_codes(provider_contract_codes)
        page_limit = _page_limit(max_pages)
        pages: list[BrokerResponse] = []
        continuation = False
        for _ in range(page_limit):
            response = await self._read_contract_detail_page(
                credentials=credentials,
                provider_contract_codes=contract_codes,
                continuation=continuation,
            )
            if response.status != 200:
                raise KisIncompleteContractSnapshot(
                    "KIS contract snapshot page was not successful"
                )
            _require_contract_snapshot_success_envelope(response)
            pages.append(response)
            if not _contract_snapshot_continues(response):
                return tuple(pages)
            continuation = True
        raise KisIncompleteContractSnapshot(
            "KIS contract snapshot exceeded the declared page limit"
        )

    async def _read_contract_detail_page(
        self,
        *,
        credentials: KisReadCredentials,
        provider_contract_codes: tuple[str, ...],
        continuation: bool,
    ) -> BrokerResponse:
        query = urlencode(
            [("QRY_CNT", str(len(provider_contract_codes)))]
            + [
                (f"SRS_CD_{index:02d}", code)
                for index, code in enumerate(provider_contract_codes, start=1)
            ]
            + [
                (f"SRS_CD_{index:02d}", "")
                for index in range(len(provider_contract_codes) + 1, 33)
            ]
        )
        headers = [
            ("authorization", f"Bearer {credentials.access_token}"),
            ("appkey", credentials.app_key),
            ("appsecret", credentials.app_secret),
            ("tr_id", self._contract_detail_tr_id),
            ("custtype", "P"),
        ]
        if continuation:
            headers.append(("tr_cont", "N"))
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"{self._contract_detail_path}?{query}",
                headers=tuple(headers),
            )
        )

    async def read_open_positions(
        self,
        *,
        credentials: KisAccountReadCredentials,
        futures_option_division: object,
    ) -> BrokerResponse:
        response = await self._read_open_position_page(
            credentials=credentials,
            futures_option_division=_futures_option_division(futures_option_division),
            continuation=False,
        )
        if response.status != 200:
            raise KisIncompleteAccountSnapshot(
                "KIS position snapshot page was not successful"
            )
        if response.header("tr_cont") == "M":
            raise KisIncompleteAccountSnapshot(
                "KIS position snapshot has an uncollected continuation page"
            )
        return response

    async def read_complete_open_positions(
        self,
        *,
        credentials: KisAccountReadCredentials,
        futures_option_division: object,
        max_pages: object,
    ) -> tuple[BrokerResponse, ...]:
        page_limit = _page_limit(max_pages)
        division = _futures_option_division(futures_option_division)
        pages: list[BrokerResponse] = []
        continuation = False
        for _ in range(page_limit):
            response = await self._read_open_position_page(
                credentials=credentials,
                futures_option_division=division,
                continuation=continuation,
            )
            if response.status != 200:
                raise KisIncompleteAccountSnapshot(
                    "KIS position snapshot page was not successful"
                )
            pages.append(response)
            if response.header("tr_cont") != "M":
                return tuple(pages)
            continuation = True
        raise KisIncompleteAccountSnapshot(
            "KIS position snapshot exceeded the declared page limit"
        )

    async def _read_open_position_page(
        self,
        *,
        credentials: KisAccountReadCredentials,
        futures_option_division: str,
        continuation: bool,
    ) -> BrokerResponse:
        query = urlencode(
            (
                ("CANO", credentials.account_number),
                ("ACNT_PRDT_CD", credentials.product_code),
                ("FUOP_DVSN", futures_option_division),
                ("CTX_AREA_FK100", ""),
                ("CTX_AREA_NK100", ""),
            )
        )
        headers = [
            ("authorization", f"Bearer {credentials.access_token}"),
            ("appkey", credentials.app_key),
            ("appsecret", credentials.app_secret),
            ("tr_id", self._open_position_tr_id),
            ("custtype", "P"),
        ]
        if continuation:
            headers.append(("tr_cont", "N"))
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"{self._open_position_path}?{query}",
                headers=tuple(headers),
            )
        )

    async def read_complete_daily_orders(
        self,
        *,
        credentials: KisAccountReadCredentials,
        start_date: date,
        end_date: date,
        execution_status: object,
        side: object,
        futures_option_division: object,
        max_pages: object,
    ) -> tuple[BrokerResponse, ...]:
        if (
            type(start_date) is not date
            or type(end_date) is not date
            or start_date > end_date
        ):
            raise ValueError("KIS daily orders require a valid date range")
        page_limit = _page_limit(max_pages)
        status = _daily_order_execution_status(execution_status)
        order_side = _daily_order_side(side)
        division = _futures_option_division(futures_option_division)
        pages: list[BrokerResponse] = []
        continuation = False
        for _ in range(page_limit):
            response = await self._read_daily_order_page(
                credentials=credentials,
                start_date=start_date,
                end_date=end_date,
                execution_status=status,
                side=order_side,
                futures_option_division=division,
                continuation=continuation,
            )
            if response.status != 200:
                raise KisIncompleteAccountSnapshot(
                    "KIS daily order snapshot page was not successful"
                )
            pages.append(response)
            if response.header("tr_cont") != "M":
                return tuple(pages)
            continuation = True
        raise KisIncompleteAccountSnapshot(
            "KIS daily order snapshot exceeded the declared page limit"
        )

    async def _read_daily_order_page(
        self,
        *,
        credentials: KisAccountReadCredentials,
        start_date: date,
        end_date: date,
        execution_status: str,
        side: str,
        futures_option_division: str,
        continuation: bool,
    ) -> BrokerResponse:
        query = urlencode(
            (
                ("CANO", credentials.account_number),
                ("ACNT_PRDT_CD", credentials.product_code),
                ("STRT_DT", f"{start_date:%Y%m%d}"),
                ("END_DT", f"{end_date:%Y%m%d}"),
                ("FM_PDGR_CD", ""),
                ("CCLD_NCCS_DVSN", execution_status),
                ("SLL_BUY_DVSN_CD", side),
                ("FUOP_DVSN", futures_option_division),
                ("CTX_AREA_FK200", ""),
                ("CTX_AREA_NK200", ""),
            )
        )
        headers = [
            ("authorization", f"Bearer {credentials.access_token}"),
            ("appkey", credentials.app_key),
            ("appsecret", credentials.app_secret),
            ("tr_id", self._daily_order_tr_id),
            ("custtype", "P"),
        ]
        if continuation:
            headers.append(("tr_cont", "N"))
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"{self._daily_order_path}?{query}",
                headers=tuple(headers),
            )
        )

    async def read_price(
        self,
        *,
        evidence_id: UUID,
        instrument_id: UUID,
        now: datetime,
        credentials: KisReadCredentials,
    ) -> BrokerResponse:
        if (
            evidence_id.version != 7
            or instrument_id.version != 7
            or now.tzinfo is not UTC
            or now.utcoffset() != UTC.utcoffset(now)
        ):
            raise UnsupportedBrokerInstrument(
                "KIS quote requires exact active evidence"
            )
        contract = await self._contract_reader.load_active(
            evidence_id=evidence_id,
            now=now,
        )
        if (
            contract.evidence_id != evidence_id
            or contract.instrument_id != instrument_id
        ):
            raise UnsupportedBrokerInstrument(
                "KIS contract master evidence does not match requested instrument"
            )
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"{self._price_path}?SRS_CD={contract.provider_contract_code}",
                headers=(
                    ("authorization", f"Bearer {credentials.access_token}"),
                    ("appkey", credentials.app_key),
                    ("appsecret", credentials.app_secret),
                    ("tr_id", self._price_tr_id),
                    ("custtype", "P"),
                ),
            )
        )

    async def read_minute_chart(
        self,
        *,
        evidence_id: UUID,
        instrument_id: UUID,
        now: datetime,
        credentials: KisReadCredentials,
        start_date: date,
        close_date: date,
        interval: KisMinuteChartInterval,
        count: int,
    ) -> BrokerResponse:
        return await self._read_minute_chart_page(
            evidence_id=evidence_id,
            instrument_id=instrument_id,
            now=now,
            credentials=credentials,
            start_date=start_date,
            close_date=close_date,
            interval=interval,
            count=count,
            query_type="Q",
            index_key="",
        )

    async def read_next_minute_chart_page(
        self,
        *,
        evidence_id: UUID,
        instrument_id: UUID,
        now: datetime,
        credentials: KisReadCredentials,
        start_date: date,
        close_date: date,
        interval: KisMinuteChartInterval,
        count: int,
        index_key: str,
    ) -> BrokerResponse:
        """Reads one provider-directed continuation page; it never recurses."""
        return await self._read_minute_chart_page(
            evidence_id=evidence_id,
            instrument_id=instrument_id,
            now=now,
            credentials=credentials,
            start_date=start_date,
            close_date=close_date,
            interval=interval,
            count=count,
            query_type="P",
            index_key=_continuation_key(index_key),
        )

    async def read_complete_minute_chart(
        self,
        *,
        evidence_id: UUID,
        instrument_id: UUID,
        now: datetime,
        credentials: KisReadCredentials,
        start_date: date,
        close_date: date,
        interval: KisMinuteChartInterval,
        count: int,
        max_pages: object,
    ) -> tuple[BrokerResponse, ...]:
        page_limit = _page_limit(max_pages)
        response = await self.read_minute_chart(
            evidence_id=evidence_id,
            instrument_id=instrument_id,
            now=now,
            credentials=credentials,
            start_date=start_date,
            close_date=close_date,
            interval=interval,
            count=count,
        )
        pages = [response]
        seen_keys: set[str] = set()
        for _ in range(page_limit - 1):
            try:
                continuation_key = decode_kis_minute_chart_page(
                    response
                ).continuation_key
            except ValueError as error:
                raise KisIncompleteMinuteChartSnapshot(
                    "KIS minute chart page is incomplete"
                ) from error
            if continuation_key is None:
                return tuple(pages)
            if continuation_key in seen_keys:
                raise KisIncompleteMinuteChartSnapshot(
                    "KIS minute chart continuation repeats"
                )
            seen_keys.add(continuation_key)
            response = await self.read_next_minute_chart_page(
                evidence_id=evidence_id,
                instrument_id=instrument_id,
                now=now,
                credentials=credentials,
                start_date=start_date,
                close_date=close_date,
                interval=interval,
                count=count,
                index_key=continuation_key,
            )
            pages.append(response)
        try:
            if decode_kis_minute_chart_page(response).continuation_key is None:
                return tuple(pages)
        except ValueError as error:
            raise KisIncompleteMinuteChartSnapshot(
                "KIS minute chart page is incomplete"
            ) from error
        raise KisIncompleteMinuteChartSnapshot(
            "KIS minute chart exceeded the declared page limit"
        )

    async def _read_minute_chart_page(
        self,
        *,
        evidence_id: UUID,
        instrument_id: UUID,
        now: datetime,
        credentials: KisReadCredentials,
        start_date: date,
        close_date: date,
        interval: KisMinuteChartInterval,
        count: int,
        query_type: str,
        index_key: str,
    ) -> BrokerResponse:
        if (
            type(start_date) is not date
            or type(close_date) is not date
            or start_date > close_date
            or type(interval) is not KisMinuteChartInterval
            or isinstance(count, bool)
            or count <= 0
        ):
            raise ValueError("KIS minute chart requires a valid date range and count")
        if (
            evidence_id.version != 7
            or instrument_id.version != 7
            or now.tzinfo is not UTC
            or now.utcoffset() != UTC.utcoffset(now)
        ):
            raise UnsupportedBrokerInstrument(
                "KIS minute chart requires exact active evidence"
            )
        contract = await self._contract_reader.load_active(
            evidence_id=evidence_id,
            now=now,
        )
        if (
            contract.evidence_id != evidence_id
            or contract.instrument_id != instrument_id
        ):
            raise UnsupportedBrokerInstrument(
                "KIS contract master evidence does not match requested instrument"
            )
        query = urlencode(
            (
                ("SRS_CD", contract.provider_contract_code),
                ("EXCH_CD", contract.provider_exchange_code),
                ("START_DATE_TIME", f"{start_date:%Y%m%d}"),
                ("CLOSE_DATE_TIME", f"{close_date:%Y%m%d}"),
                ("QRY_TP", query_type),
                ("QRY_CNT", str(count)),
                ("QRY_GAP", interval.value),
                ("INDEX_KEY", index_key),
            )
        )
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"{self._minute_chart_path}?{query}",
                headers=(
                    ("authorization", f"Bearer {credentials.access_token}"),
                    ("appkey", credentials.app_key),
                    ("appsecret", credentials.app_secret),
                    ("tr_id", self._minute_chart_tr_id),
                    ("custtype", "P"),
                ),
            )
        )

    async def submit(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("KIS write adapter is not enabled")

    async def cancel(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("KIS write adapter is not enabled")

    async def replace(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("KIS write adapter is not enabled")


class KisCashExecutionAdapter:
    """Explicit write-capable adapter; the read-only adapter remains disabled."""

    def __init__(self, *, writer: KisCashWriter) -> None:
        if type(writer) is not KisCashWriter:
            raise TypeError("exact KIS cash writer is required")
        self._writer = writer

    async def submit(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        return await self._writer.submit(command)

    async def cancel(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        return await self._writer.cancel(command)

    async def replace(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        return await self._writer.replace(command)

    async def recover_submit(
        self, command: BrokerOrderCommand, *, now: datetime
    ) -> BrokerWriteResult | None:
        return await self._writer.recover_submit(command, now=now)


def decode_kis_minute_chart_page(response: BrokerResponse) -> KisMinuteChartPage:
    """Decodes KIS raw fields but never assigns a timezone or bar-volume meaning."""
    if response.status != 200:
        raise ValueError("KIS minute chart response is not successful")
    try:
        payload = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("KIS minute chart response is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("KIS minute chart response is not an object")
    payload = cast(Mapping[str, object], payload)
    metadata = payload.get("output1")
    records_payload = payload.get("output2")
    if not isinstance(metadata, Mapping) or not isinstance(records_payload, list):
        raise ValueError("KIS minute chart response has invalid output fields")
    metadata = cast(Mapping[str, object], metadata)
    records_payload = cast(list[object], records_payload)
    expected_count = _provider_record_count(metadata.get("ret_cnt"))
    records = tuple(_minute_chart_record(record) for record in records_payload)
    if expected_count != len(records):
        raise ValueError("KIS minute chart provider record count does not match")
    index_key = metadata.get("index_key")
    if not isinstance(index_key, str):
        raise ValueError("KIS minute chart continuation key is invalid")
    return KisMinuteChartPage(
        records=records,
        continuation_key=None if not index_key else _continuation_key(index_key),
    )


def decode_kis_futures_price_page(response: BrokerResponse) -> KisFuturesPricePage:
    """Decodes KIS quote fields without assigning UTC or completed-bar semantics."""
    if response.status != 200:
        raise ValueError("KIS futures price response is not successful")
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("KIS futures price response is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("KIS futures price response is not an object")
    payload = cast(Mapping[str, object], payload)
    output = payload.get("output1")
    records_payload: tuple[object, ...]
    if isinstance(output, Mapping):
        records_payload = (cast(Mapping[str, object], output),)
    elif isinstance(output, list):
        records_payload = tuple(cast(list[object], output))
    else:
        raise ValueError("KIS futures price response output is invalid")
    return KisFuturesPricePage(
        records=tuple(_futures_price_record(record) for record in records_payload)
    )


def decode_kis_contract_detail_page(response: BrokerResponse) -> KisContractDetailPage:
    """Decodes KIS `srs_cd` observations without selecting a contract."""
    if response.status != 200:
        raise ValueError("KIS contract detail response is not successful")
    return KisContractDetailPage.from_provider_body(body=response.body)


def _minute_chart_record(payload: object) -> KisMinuteChartRecord:
    if not isinstance(payload, Mapping):
        raise ValueError("KIS minute chart record is invalid")
    payload = cast(Mapping[str, object], payload)
    date_text = _provider_text(payload.get("data_date"), name="data_date")
    time_text = _provider_text(payload.get("data_time"), name="data_time")
    try:
        local_timestamp = datetime.strptime(f"{date_text}{time_text}", "%Y%m%d%H%M%S")
    except ValueError as error:
        raise ValueError("KIS minute chart record timestamp is invalid") from error
    if local_timestamp.strftime("%Y%m%d%H%M%S") != f"{date_text}{time_text}":
        raise ValueError("KIS minute chart record timestamp is invalid")
    return KisMinuteChartRecord(
        trading_date=local_timestamp.date(),
        trading_time=local_timestamp.time(),
        open_price=_provider_decimal(payload.get("open_price"), name="open_price"),
        high_price=_provider_decimal(payload.get("high_price"), name="high_price"),
        low_price=_provider_decimal(payload.get("low_price"), name="low_price"),
        close_price=_provider_decimal(payload.get("last_price"), name="last_price"),
        cumulative_volume=_provider_decimal(payload.get("vol"), name="vol"),
    )


def _futures_price_record(payload: object) -> KisFuturesPriceRecord:
    if not isinstance(payload, Mapping):
        raise ValueError("KIS futures price record is invalid")
    payload = cast(Mapping[str, object], payload)
    date_text = _provider_text(payload.get("proc_date"), name="proc_date")
    time_text = _provider_text(payload.get("proc_time"), name="proc_time")
    try:
        provider_timestamp = datetime.strptime(
            f"{date_text}{time_text}", "%Y%m%d%H%M%S"
        )
    except ValueError as error:
        raise ValueError("KIS futures price timestamp is invalid") from error
    if provider_timestamp.strftime("%Y%m%d%H%M%S") != f"{date_text}{time_text}":
        raise ValueError("KIS futures price timestamp is invalid")
    return KisFuturesPriceRecord(
        provider_date=provider_timestamp.date(),
        provider_time=provider_timestamp.time(),
        open_price=_provider_decimal(payload.get("open_price"), name="open_price"),
        high_price=_provider_decimal(payload.get("high_price"), name="high_price"),
        low_price=_provider_decimal(payload.get("low_price"), name="low_price"),
        last_price=_provider_decimal(payload.get("last_price"), name="last_price"),
        cumulative_volume=_provider_decimal(payload.get("vol"), name="vol"),
        exchange_code=_provider_text(payload.get("exch_cd"), name="exch_cd"),
        currency_code=_provider_text(payload.get("crc_cd"), name="crc_cd"),
    )


def _provider_record_count(value: object) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise ValueError("KIS minute chart provider record count is invalid")
    return int(value)


def _provider_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"KIS minute chart {name} is invalid")
    return value


def _provider_decimal(value: object, *, name: str) -> Decimal:
    try:
        return parse_contract_decimal(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"KIS minute chart {name} is invalid") from error


def _continuation_key(value: object) -> str:
    if not isinstance(value, str) or not value or "\n" in value:
        raise ValueError("KIS minute chart index key must be a non-empty single line")
    return value


def _futures_option_division(value: object) -> str:
    if value not in {"00", "01", "02"}:
        raise ValueError("KIS futures option division must be 00, 01, or 02")
    return cast(str, value)


def _daily_order_execution_status(value: object) -> str:
    if value not in {"01", "02", "03"}:
        raise ValueError("KIS daily order execution status must be 01, 02, or 03")
    return cast(str, value)


def _daily_order_side(value: object) -> str:
    if value not in {"%%", "01", "02"}:
        raise ValueError("KIS daily order side must be %%, 01, or 02")
    return cast(str, value)


def _kis_order_side(value: object) -> str:
    if value is Side.BUY:
        return "02"
    if value is Side.SELL:
        return "01"
    raise ValueError("KIS futures preview side is invalid")


def _kis_order_terms(command: BrokerOrderCommand) -> tuple[str, str, str]:
    if command.order_style is OrderStyle.MARKET:
        if command.limit_price is not None:
            raise ValueError("KIS market preview cannot carry a limit price")
        return "2", "", "2"
    if command.order_style is OrderStyle.LIMIT:
        if command.limit_price is None:
            raise ValueError("KIS limit preview requires a limit price")
        limit_price = require_decimal(command.limit_price)
        if limit_price <= 0:
            raise ValueError("KIS limit preview requires a positive limit price")
        return "1", decimal_to_string(limit_price), "6"
    raise ValueError("KIS futures preview order style is invalid")


def _contract_detail_code(value: object) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("KIS contract detail record is invalid")
    payload = cast(Mapping[str, object], value)
    return _provider_contract_codes((payload.get("srs_cd"),))[0]


def _provider_contract_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError("KIS contract detail requires one through 32 provider codes")
    codes = cast(tuple[object, ...], value)
    if not 1 <= len(codes) <= 32:
        raise ValueError("KIS contract detail requires one through 32 provider codes")
    if any(
        not isinstance(code, str)
        or code in {"NQ", "MNQ"}
        or not code.isascii()
        or not code.isalnum()
        or code != code.upper()
        for code in codes
    ):
        raise ValueError(
            "KIS contract detail codes must be explicit uppercase ASCII "
            "alphanumeric values"
        )
    return cast(tuple[str, ...], codes)


def _contract_snapshot_continues(response: BrokerResponse) -> bool:
    continuation = response.header("tr_cont")
    if continuation in {"M", "F"}:
        return True
    if continuation in {None, ""}:
        return False
    raise KisIncompleteContractSnapshot("KIS contract snapshot continuation is invalid")


def _require_contract_snapshot_success_envelope(response: BrokerResponse) -> None:
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KisIncompleteContractSnapshot(
            "KIS contract snapshot has an invalid success envelope"
        ) from error
    if not isinstance(payload, Mapping):
        raise KisIncompleteContractSnapshot(
            "KIS contract snapshot has an invalid success envelope"
        )
    payload = cast(Mapping[str, object], payload)
    if payload.get("rt_cd") != "0" or not isinstance(payload.get("output2"), list):
        raise KisIncompleteContractSnapshot(
            "KIS contract snapshot has an invalid success envelope"
        )


def _page_limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10:
        raise ValueError("KIS snapshot page limit must be an integer from 1 through 10")
    return value
