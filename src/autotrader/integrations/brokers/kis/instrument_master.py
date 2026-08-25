from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from autotrader.domain.krx_instrument_authority import (
    KrxCashMarket,
    KrxCommonStockAuthoritySnapshot,
    KrxCommonStockInstrumentAuthority,
)

_KOSPI_WIDTHS = (
    2,
    1,
    4,
    4,
    4,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    9,
    5,
    5,
    1,
    1,
    1,
    2,
    1,
    1,
    1,
    2,
    2,
    2,
    3,
    1,
    3,
    12,
    12,
    8,
    15,
    21,
    2,
    7,
    1,
    1,
    1,
    1,
    1,
    9,
    9,
    9,
    5,
    9,
    8,
    9,
    3,
    1,
    1,
    1,
)
_KOSDAQ_WIDTHS = (
    2,
    1,
    4,
    4,
    4,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    9,
    5,
    5,
    1,
    1,
    1,
    2,
    1,
    1,
    1,
    2,
    2,
    2,
    3,
    1,
    3,
    12,
    12,
    8,
    15,
    21,
    2,
    7,
    1,
    1,
    1,
    1,
    9,
    9,
    9,
    5,
    9,
    8,
    9,
    3,
    1,
    1,
    1,
)


class KisIncompleteInstrumentMaster(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _MasterFormat:
    market: KrxCashMarket
    widths: tuple[int, ...]
    etp_index: int
    preferred_index: int

    @property
    def tail_width(self) -> int:
        return sum(self.widths)


_KOSPI = _MasterFormat(KrxCashMarket.KOSPI, _KOSPI_WIDTHS, 12, 54)
_KOSDAQ = _MasterFormat(KrxCashMarket.KOSDAQ, _KOSDAQ_WIDTHS, 8, 49)


def parse_kis_krx_common_stock_authority(
    kospi_master: bytes,
    kosdaq_master: bytes,
    *,
    captured_at: datetime,
) -> KrxCommonStockAuthoritySnapshot:
    try:
        if type(kospi_master) is not bytes or type(kosdaq_master) is not bytes:
            raise ValueError("master inputs must be exact bytes")
        kospi_facts, kospi_symbols = _parse_master(kospi_master, _KOSPI)
        kosdaq_facts, kosdaq_symbols = _parse_master(kosdaq_master, _KOSDAQ)
        if not kospi_facts or not kosdaq_facts:
            raise ValueError("each master must contain a common-stock fact")
        if kospi_symbols & kosdaq_symbols:
            raise ValueError("master short codes must be globally unique")
        instruments = tuple(
            sorted((*kospi_facts, *kosdaq_facts), key=lambda fact: fact.symbol)
        )
        return KrxCommonStockAuthoritySnapshot.build(
            captured_at=captured_at,
            kospi_source_hash=sha256(kospi_master).digest(),
            kosdaq_source_hash=sha256(kosdaq_master).digest(),
            instruments=instruments,
        )
    except UnicodeError, TypeError, ValueError:
        raise KisIncompleteInstrumentMaster(
            "KIS KRX instrument master is incomplete"
        ) from None


def _parse_master(
    source: bytes,
    master_format: _MasterFormat,
) -> tuple[list[KrxCommonStockInstrumentAuthority], set[str]]:
    text = source.decode("cp949")
    lines = text.splitlines()
    if not lines:
        raise ValueError("master must contain records")
    facts: list[KrxCommonStockInstrumentAuthority] = []
    symbols: set[str] = set()
    for line in lines:
        if len(line) <= master_format.tail_width + 21:
            raise ValueError("master record is truncated")
        prefix = line[: -master_format.tail_width]
        tail = line[-master_format.tail_width :]
        symbol = prefix[:9].rstrip()
        standard_code = prefix[9:21].rstrip()
        name = prefix[21:].strip()
        if (
            not symbol
            or len(symbol) > 9
            or not symbol.isascii()
            or not symbol.isalnum()
            or symbol in symbols
            or not name
        ):
            raise ValueError("master record identity is invalid")
        symbols.add(symbol)
        fields = _split_fields(tail, master_format.widths)
        if not _is_positive_common_stock(
            symbol=symbol,
            fields=fields,
            master_format=master_format,
        ):
            continue
        facts.append(
            KrxCommonStockInstrumentAuthority(
                market=master_format.market,
                symbol=symbol,
                standard_code=standard_code,
                name=name,
                security_group_code=fields[0],
                etp_product_class_code=fields[master_format.etp_index],
                preferred_stock_class_code=fields[master_format.preferred_index],
            )
        )
    return facts, symbols


def _split_fields(tail: str, widths: tuple[int, ...]) -> tuple[str, ...]:
    fields: list[str] = []
    offset = 0
    for width in widths:
        fields.append(tail[offset : offset + width].strip())
        offset += width
    if offset != len(tail):
        raise ValueError("master tail width is invalid")
    return tuple(fields)


def _is_positive_common_stock(
    *,
    symbol: str,
    fields: tuple[str, ...],
    master_format: _MasterFormat,
) -> bool:
    return (
        len(symbol) == 6
        and symbol.isdigit()
        and fields[0] == "ST"
        and fields[master_format.etp_index] in {"", "0"}
        and fields[master_format.preferred_index] == "0"
    )
