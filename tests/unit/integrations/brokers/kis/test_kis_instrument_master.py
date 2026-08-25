from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from autotrader.domain.krx_instrument_authority import (
    KrxCashMarket,
    KrxCommonStockAuthoritySnapshot,
    KrxCommonStockInstrumentAuthority,
)
from autotrader.integrations.brokers.kis.instrument_master import (
    KisIncompleteInstrumentMaster,
    parse_kis_krx_common_stock_authority,
)

EVIDENCE = (
    Path(__file__).resolve().parents[5] / "docs/providers/kis/"
    "b093e42-krx-common-stock-master-authority.sanitized.json"
)
CAPTURED_AT = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
KOSPI_WIDTHS = (
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
KOSDAQ_WIDTHS = (
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


def _master_record(
    *,
    market: KrxCashMarket,
    symbol: str,
    standard_code: str,
    name: str,
    group: str,
    etp: str,
    preferred: str,
) -> bytes:
    if market is KrxCashMarket.KOSPI:
        widths = KOSPI_WIDTHS
        etp_index = 12
        preferred_index = 54
    else:
        widths = KOSDAQ_WIDTHS
        etp_index = 8
        preferred_index = 49
    fields = [" " * width for width in widths]
    fields[0] = group.ljust(widths[0])
    fields[etp_index] = etp.ljust(widths[etp_index])
    fields[preferred_index] = preferred.ljust(widths[preferred_index])
    line = symbol.ljust(9) + standard_code.ljust(12) + name + "".join(fields) + "\n"
    return line.encode("cp949")


def _common_record(
    market: KrxCashMarket,
    symbol: str,
    standard_code: str,
    name: str,
) -> bytes:
    return _master_record(
        market=market,
        symbol=symbol,
        standard_code=standard_code,
        name=name,
        group="ST",
        etp="",
        preferred="0",
    )


def _valid_masters() -> tuple[bytes, bytes]:
    return (
        _common_record(KrxCashMarket.KOSPI, "005930", "KR7005930003", "삼성전자"),
        _common_record(KrxCashMarket.KOSDAQ, "035720", "KR7035720002", "카카오"),
    )


def test_official_master_evidence_is_pinned() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert evidence["officialRevision"] == ("b093e42ba32d1df5f5ddad7a71cb715cbc800832")
    assert evidence["sources"] == [
        {
            "path": "stocks_info/kis_kospi_code_mst.py",
            "contentUrl": (
                "https://raw.githubusercontent.com/koreainvestment/"
                "open-trading-api/b093e42ba32d1df5f5ddad7a71cb715cbc800832/"
                "stocks_info/kis_kospi_code_mst.py"
            ),
            "sha256": (
                "135ed22451832935f3d962d1997a05726aaec84cc9d1472ff40ea24685edd0d4"
            ),
            "hashOf": "RAW_FILE_BYTES",
        },
        {
            "path": "stocks_info/kis_kosdaq_code_mst.py",
            "contentUrl": (
                "https://raw.githubusercontent.com/koreainvestment/"
                "open-trading-api/b093e42ba32d1df5f5ddad7a71cb715cbc800832/"
                "stocks_info/kis_kosdaq_code_mst.py"
            ),
            "sha256": (
                "fcacb0f401c745bf1810c731a1c533d16ff6af83729ca0771faab876cd0e56bc"
            ),
            "hashOf": "RAW_FILE_BYTES",
        },
        {
            "path": "stocks_info/종목마스터정보(코스피).h",
            "contentUrl": (
                "https://raw.githubusercontent.com/koreainvestment/"
                "open-trading-api/b093e42ba32d1df5f5ddad7a71cb715cbc800832/"
                "stocks_info/%EC%A2%85%EB%AA%A9%EB%A7%88%EC%8A%A4%ED%84%B0%"
                "EC%A0%95%EB%B3%B4%28%EC%BD%94%EC%8A%A4%ED%94%BC%29.h"
            ),
            "sha256": (
                "383cb7a4bb6f7359bc742781afd18f87c95e9a939502d2e548593a1be0de24e4"
            ),
            "hashOf": "RAW_FILE_BYTES",
        },
        {
            "path": "stocks_info/종목마스터정보(코스닥).h",
            "contentUrl": (
                "https://raw.githubusercontent.com/koreainvestment/"
                "open-trading-api/b093e42ba32d1df5f5ddad7a71cb715cbc800832/"
                "stocks_info/%EC%A2%85%EB%AA%A9%EB%A7%88%EC%8A%A4%ED%84%B0%"
                "EC%A0%95%EB%B3%B4%28%EC%BD%94%EC%8A%A4%EB%8B%A5%29.h"
            ),
            "sha256": (
                "d1660f8a3829cff4bcfc5c4e4f4bce588fa36f6f3dd388471c70d0776ea139d4"
            ),
            "hashOf": "RAW_FILE_BYTES",
        },
    ]
    assert evidence["masters"] == {
        "KOSPI": {
            "downloadUrl": (
                "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
            ),
            "encoding": "CP949",
            "tailWidth": 227,
            "fieldWidths": [
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
            ],
            "fieldIndexes": {
                "securityGroupCode": 0,
                "etpProductClassCode": 12,
                "preferredStockClassCode": 54,
            },
        },
        "KOSDAQ": {
            "downloadUrl": (
                "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip"
            ),
            "encoding": "CP949",
            "tailWidth": 221,
            "fieldWidths": [
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
            ],
            "fieldIndexes": {
                "securityGroupCode": 0,
                "etpProductClassCode": 8,
                "preferredStockClassCode": 49,
            },
        },
    }
    assert evidence["positivePredicate"] == {
        "shortCode": "EXACTLY_SIX_ASCII_DIGITS",
        "securityGroupCode": "ST",
        "etpProductClassCode": ["", "0"],
        "preferredStockClassCode": "0",
    }
    assert evidence["observedDynamicMasters"] == {
        "capturedAt": "2026-08-18",
        "KOSPI": {
            "zipSha256": (
                "37e72a85e3d040972d110d734238dc8d74b56e771e3cc0876077c8985729e720"
            ),
            "member": "kospi_code.mst",
            "memberSha256": (
                "66d754df700d736de3bfebcccd9e48c5672d97400f381d66c3e3f10e3f54e5b6"
            ),
            "recordCount": 2558,
            "nonEtpEncodingObserved": "BLANK",
        },
        "KOSDAQ": {
            "zipSha256": (
                "5cd1d1480afd9def62e343ba03c2e9e7c4db06493639ed21f9a2ce14f73d740d"
            ),
            "member": "kosdaq_code.mst",
            "memberSha256": (
                "73c95ab9dc1bdf95c6b3e5d17ebae890edbb69fb8812188d68d4553b8ab03873"
            ),
            "recordCount": 1823,
            "nonEtpEncodingObserved": "BLANK",
        },
    }
    assert evidence["runtime"] == "BLOCKED_SOURCE_FRESHNESS_AND_ACTIVATION"


def test_parses_only_exact_common_stock_predicate_from_both_masters() -> None:
    kospi, kosdaq = _valid_masters()
    kospi += b"".join(
        (
            _master_record(
                market=KrxCashMarket.KOSPI,
                symbol="069500",
                standard_code="KR7069500007",
                name="ETF",
                group="EF",
                etp="2",
                preferred="0",
            ),
            _master_record(
                market=KrxCashMarket.KOSPI,
                symbol="Q500001",
                standard_code="KRG500000001",
                name="ETN",
                group="EN",
                etp="3",
                preferred="0",
            ),
            _master_record(
                market=KrxCashMarket.KOSPI,
                symbol="005935",
                standard_code="KR7005931001",
                name="우선주",
                group="ST",
                etp="",
                preferred="1",
            ),
            _master_record(
                market=KrxCashMarket.KOSPI,
                symbol="088980",
                standard_code="KR7088980008",
                name="리츠",
                group="RT",
                etp="",
                preferred="0",
            ),
        )
    )

    snapshot = parse_kis_krx_common_stock_authority(
        kospi,
        kosdaq,
        captured_at=CAPTURED_AT,
    )

    assert snapshot.captured_at is CAPTURED_AT
    assert snapshot.kospi_source_hash == sha256(kospi).digest()
    assert snapshot.kosdaq_source_hash == sha256(kosdaq).digest()
    assert snapshot.instruments == (
        KrxCommonStockInstrumentAuthority(
            market=KrxCashMarket.KOSPI,
            symbol="005930",
            standard_code="KR7005930003",
            name="삼성전자",
            security_group_code="ST",
            etp_product_class_code="",
            preferred_stock_class_code="0",
        ),
        KrxCommonStockInstrumentAuthority(
            market=KrxCashMarket.KOSDAQ,
            symbol="035720",
            standard_code="KR7035720002",
            name="카카오",
            security_group_code="ST",
            etp_product_class_code="",
            preferred_stock_class_code="0",
        ),
    )
    assert len(snapshot.source_hash) == 32


def test_common_stock_authority_preserves_official_name_and_binds_it() -> None:
    kospi, kosdaq = _valid_masters()
    snapshot = parse_kis_krx_common_stock_authority(
        kospi,
        kosdaq,
        captured_at=CAPTURED_AT,
    )

    assert tuple(instrument.name for instrument in snapshot.instruments) == (
        "삼성전자",
        "카카오",
    )
    forged = replace(snapshot.instruments[0], name="변조종목")
    with pytest.raises(ValueError, match="source_hash"):
        replace(
            snapshot,
            instruments=(forged, *snapshot.instruments[1:]),
        )


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "삼성\r전자",
        "삼성\n전자",
        "삼성\0전자",
        "삼성\x1f전자",
        "가" * 257,
        123,
    ],
)
def test_common_stock_authority_rejects_invalid_official_name(name: object) -> None:
    with pytest.raises(ValueError, match="name"):
        KrxCommonStockInstrumentAuthority(
            market=KrxCashMarket.KOSPI,
            symbol="005930",
            standard_code="KR7005930003",
            name=cast(str, name),
            security_group_code="ST",
            etp_product_class_code="0",
            preferred_stock_class_code="0",
        )


def test_etp_class_is_an_independent_gate_and_zero_means_non_etp() -> None:
    kospi = _common_record(
        KrxCashMarket.KOSPI, "005930", "KR7005930003", "삼성전자"
    ) + _master_record(
        market=KrxCashMarket.KOSPI,
        symbol="123456",
        standard_code="KR7123456006",
        name="ST형ETP",
        group="ST",
        etp="2",
        preferred="0",
    )
    kosdaq = _master_record(
        market=KrxCashMarket.KOSDAQ,
        symbol="035720",
        standard_code="KR7035720002",
        name="카카오",
        group="ST",
        etp="0",
        preferred="0",
    )

    snapshot = parse_kis_krx_common_stock_authority(
        kospi,
        kosdaq,
        captured_at=CAPTURED_AT,
    )

    assert tuple(fact.symbol for fact in snapshot.instruments) == (
        "005930",
        "035720",
    )
    assert snapshot.instruments[1].etp_product_class_code == "0"


@pytest.mark.parametrize(
    "case",
    ["empty", "invalid-cp949", "truncated", "no-positive-facts"],
)
def test_malformed_or_empty_authority_fails_closed(case: str) -> None:
    kospi, kosdaq = _valid_masters()
    if case == "empty":
        kospi = b""
    elif case == "invalid-cp949":
        kospi = b"\xff"
    elif case == "truncated":
        kospi = b"short\n"
    else:
        kospi = _master_record(
            market=KrxCashMarket.KOSPI,
            symbol="069500",
            standard_code="KR7069500007",
            name="ETF",
            group="EF",
            etp="2",
            preferred="0",
        )
        kosdaq = _master_record(
            market=KrxCashMarket.KOSDAQ,
            symbol="035725",
            standard_code="KR7035721000",
            name="우선주",
            group="ST",
            etp="",
            preferred="2",
        )

    with pytest.raises(KisIncompleteInstrumentMaster, match="incomplete"):
        parse_kis_krx_common_stock_authority(
            kospi,
            kosdaq,
            captured_at=CAPTURED_AT,
        )


def test_duplicate_symbol_across_masters_fails_closed() -> None:
    duplicate = "005930"
    kospi = _common_record(KrxCashMarket.KOSPI, duplicate, "KR7005930003", "삼성전자")
    kosdaq = _common_record(KrxCashMarket.KOSDAQ, duplicate, "KR7005930003", "복제")

    with pytest.raises(KisIncompleteInstrumentMaster, match="incomplete"):
        parse_kis_krx_common_stock_authority(
            kospi,
            kosdaq,
            captured_at=CAPTURED_AT,
        )


@pytest.mark.parametrize(
    "captured_at",
    [
        datetime(2026, 8, 18, 9, 0),
        datetime(2026, 8, 18, 9, 0, tzinfo=timezone(timedelta(hours=9))),
        datetime(2026, 8, 18, 9, 0, 0, 1, tzinfo=UTC),
    ],
)
def test_capture_time_must_be_exact_whole_second_utc(captured_at: datetime) -> None:
    kospi, kosdaq = _valid_masters()

    with pytest.raises(KisIncompleteInstrumentMaster, match="incomplete"):
        parse_kis_krx_common_stock_authority(
            kospi,
            kosdaq,
            captured_at=captured_at,
        )


def test_public_snapshot_recomputes_hash_and_revalidates_children() -> None:
    kospi, kosdaq = _valid_masters()
    snapshot = parse_kis_krx_common_stock_authority(
        kospi,
        kosdaq,
        captured_at=CAPTURED_AT,
    )

    with pytest.raises(ValueError, match="source_hash"):
        KrxCommonStockAuthoritySnapshot(
            captured_at=snapshot.captured_at,
            kospi_source_hash=snapshot.kospi_source_hash,
            kosdaq_source_hash=snapshot.kosdaq_source_hash,
            instruments=snapshot.instruments,
            source_hash=b"x" * 32,
        )

    forged = snapshot.instruments[0]
    object.__setattr__(forged, "symbol", "BAD")
    with pytest.raises(ValueError, match="instrument"):
        KrxCommonStockAuthoritySnapshot(
            captured_at=snapshot.captured_at,
            kospi_source_hash=snapshot.kospi_source_hash,
            kosdaq_source_hash=snapshot.kosdaq_source_hash,
            instruments=snapshot.instruments,
            source_hash=snapshot.source_hash,
        )

    coherent = parse_kis_krx_common_stock_authority(
        kospi,
        kosdaq,
        captured_at=CAPTURED_AT,
    )
    object.__setattr__(coherent.instruments[0], "symbol", "005935")
    with pytest.raises(ValueError, match="source_hash"):
        KrxCommonStockAuthoritySnapshot(
            captured_at=coherent.captured_at,
            kospi_source_hash=coherent.kospi_source_hash,
            kosdaq_source_hash=coherent.kosdaq_source_hash,
            instruments=coherent.instruments,
            source_hash=coherent.source_hash,
        )
