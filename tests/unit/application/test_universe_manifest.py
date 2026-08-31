"""Refusing a universe nobody published.

Section 11.5 rules out a row editor, so every way of getting a wrong universe
in has to go through this one document. These pin the refusals, because a
manifest that parses when it should not is a strategy quietly filtering on
something that was never a published list.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from autotrader.application.universe_manifest import (
    UniverseManifestError,
    UniverseMember,
    canonical_bytes,
    compare,
    parse_manifest,
    verify_digest,
)

PUBLISHED = "2026-08-30T09:00:00+00:00"


def _document(**overrides: object) -> str:
    payload: dict[str, object] = {
        "universe_code": "KOSPI200",
        "effective_date": "2026-08-31",
        "source": {
            "name": "KRX",
            "reference": "kospi200-constituents-20260831",
            "published_at": PUBLISHED,
        },
        "members": [
            {"symbol": "005930", "common_stock": True, "sector": "IT"},
            {"symbol": "005935", "common_stock": False, "sector": "IT"},
            {"symbol": "000660", "common_stock": True, "sector": "IT"},
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _binance(**overrides: object) -> str:
    payload: dict[str, object] = {
        "universe_code": "BINANCE_USDM",
        "effective_date": "2026-08-31",
        "source": {
            "name": "Binance",
            "reference": "fapi/v1/exchangeInfo",
            "published_at": PUBLISHED,
        },
        "members": [{"symbol": "BTCUSDT", "sector": None}],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_a_published_list_is_read_whole() -> None:
    manifest = parse_manifest(_document())

    assert manifest.universe_code == "KOSPI200"
    assert manifest.effective_date == date(2026, 8, 31)
    assert manifest.provenance.name == "KRX"
    assert manifest.provenance.published_at == datetime(2026, 8, 30, 9, tzinfo=UTC)
    assert manifest.symbols == {"005930", "005935", "000660"}


def test_a_preferred_line_is_a_member_that_is_not_a_common_share() -> None:
    """Both blockers exist and say different things. Dropping preferred lines
    at upload would turn NOT_COMMON_STOCK_AS_OF into NOT_MEMBER_AS_OF, which
    reports the wrong reason for standing aside."""
    members = {member.symbol: member for member in parse_manifest(_document()).members}

    assert members["005935"].common_stock is False
    assert members["005930"].common_stock is True


def test_a_share_class_flag_is_required_where_it_means_something() -> None:
    document = _document(
        members=[{"symbol": "005930", "sector": "IT"}],
    )

    with pytest.raises(UniverseManifestError, match="common_stock flag"):
        parse_manifest(document)


def test_a_perpetual_is_refused_a_share_class() -> None:
    """Answering true would be inventing a fact about an instrument that has
    no share class at all."""
    with pytest.raises(UniverseManifestError, match="not a share class"):
        parse_manifest(_binance(members=[{"symbol": "BTCUSDT", "common_stock": True}]))


def test_the_binance_authority_is_one_symbol() -> None:
    manifest = parse_manifest(_binance())

    assert manifest.symbols == {"BTCUSDT"}
    assert manifest.members[0].common_stock is None


def test_an_absent_sector_is_carried_rather_than_refused() -> None:
    """The strategy already blocks with SECTOR_AUTHORITY_UNAVAILABLE, which
    says more than a rejected upload."""
    manifest = parse_manifest(
        _document(members=[{"symbol": "005930", "common_stock": True}])
    )

    assert manifest.members[0].sector is None


def test_an_empty_universe_is_refused() -> None:
    with pytest.raises(UniverseManifestError, match="filters nothing"):
        parse_manifest(_document(members=[]))


def test_a_repeated_symbol_is_refused() -> None:
    """Silently keeping the last one would let a preferred line overwrite the
    common line it was listed beside."""
    document = _document(
        members=[
            {"symbol": "005930", "common_stock": True, "sector": "A"},
            {"symbol": "005930", "common_stock": False, "sector": "A"},
        ]
    )

    with pytest.raises(UniverseManifestError, match="appears twice"):
        parse_manifest(document)


def test_an_unknown_authority_is_refused() -> None:
    with pytest.raises(UniverseManifestError, match="not a known universe"):
        parse_manifest(_document(universe_code="KOSDAQ150"))


def test_a_naive_publication_time_is_refused() -> None:
    with pytest.raises(UniverseManifestError, match="offset"):
        parse_manifest(
            _document(
                source={
                    "name": "KRX",
                    "reference": "x",
                    "published_at": "2026-08-30T09:00:00",
                }
            )
        )


def test_a_document_that_is_not_json_is_refused() -> None:
    with pytest.raises(UniverseManifestError, match="not JSON"):
        parse_manifest(b"symbol,common\n005930,Y\n")


def test_the_digest_survives_reordering_and_reindenting() -> None:
    """A list round-tripped through a spreadsheet is the same authority. A
    digest that disagreed would make every re-upload look like a change."""
    original = parse_manifest(_document())
    shuffled = parse_manifest(
        _document(
            members=[
                {
                    "sector": "IT",
                    "common_stock": True,
                    "symbol": "000660",
                },
                {"symbol": "005935", "common_stock": False, "sector": "IT"},
                {"symbol": "005930", "common_stock": True, "sector": "IT"},
            ]
        )
    )

    assert shuffled.content_digest == original.content_digest


def test_one_changed_symbol_changes_the_digest() -> None:
    original = parse_manifest(_document())
    edited = parse_manifest(
        _document(
            members=[
                {"symbol": "005930", "common_stock": True, "sector": "IT"},
                {"symbol": "005935", "common_stock": False, "sector": "IT"},
                {"symbol": "035420", "common_stock": True, "sector": "Comms"},
            ]
        )
    )

    assert edited.content_digest != original.content_digest


def test_the_date_is_part_of_what_the_universe_is() -> None:
    """The same membership on a different day is a different authority, and
    point-in-time answers depend on telling them apart."""
    assert (
        parse_manifest(_document(effective_date="2026-09-01")).content_digest
        != parse_manifest(_document()).content_digest
    )


def test_where_it_came_from_is_not_part_of_the_digest() -> None:
    """Two operators fetching the same published list from different mirrors
    hold the same universe."""
    mirrored = parse_manifest(
        _document(
            source={
                "name": "Bloomberg",
                "reference": "a-different-place-entirely",
                "published_at": "2026-08-30T23:00:00+00:00",
            }
        )
    )

    assert mirrored.content_digest == parse_manifest(_document()).content_digest


def test_a_digest_the_operator_did_not_compute_from_this_file_is_refused() -> None:
    manifest = parse_manifest(_document())

    verify_digest(manifest, claimed=manifest.content_digest)
    with pytest.raises(UniverseManifestError, match="does not match"):
        verify_digest(manifest, claimed=bytes(32))


def test_a_claimed_digest_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(UniverseManifestError, match="32 bytes"):
        verify_digest(parse_manifest(_document()), claimed=b"short")


def test_a_swap_is_reported_as_both_sides() -> None:
    previous = parse_manifest(_document())
    current = parse_manifest(
        _document(
            effective_date="2026-09-30",
            members=[
                {"symbol": "005930", "common_stock": True, "sector": "IT"},
                {"symbol": "005935", "common_stock": False, "sector": "IT"},
                {"symbol": "035420", "common_stock": True, "sector": "Comms"},
            ],
        )
    )

    difference = compare(previous, current)

    assert difference.added == ("035420",)
    assert difference.removed == ("000660",)
    assert difference.identical is False


def test_a_reclassified_member_is_neither_added_nor_removed() -> None:
    """The only edit a member count cannot see."""
    previous = parse_manifest(_document())
    current = parse_manifest(
        _document(
            members=[
                {"symbol": "005930", "common_stock": True, "sector": "Semis"},
                {"symbol": "005935", "common_stock": False, "sector": "IT"},
                {"symbol": "000660", "common_stock": True, "sector": "IT"},
            ]
        )
    )

    difference = compare(previous, current)

    assert (difference.added, difference.removed) == ((), ())
    assert difference.changed == ("005930",)


def test_two_authorities_do_not_compare() -> None:
    with pytest.raises(UniverseManifestError, match="one authority"):
        compare(parse_manifest(_document()), parse_manifest(_binance()))


def test_an_unchanged_list_compares_identical() -> None:
    assert (
        compare(parse_manifest(_document()), parse_manifest(_document())).identical
        is True
    )


def test_the_canonical_bytes_are_stable_text() -> None:
    """The digest is only as reproducible as the bytes it is taken over, so
    the shape of those bytes is pinned rather than left to a default."""
    manifest = parse_manifest(
        _document(members=[{"symbol": "005930", "common_stock": True, "sector": "IT"}])
    )

    assert canonical_bytes(manifest) == (
        b'{"effective_date":"2026-08-31","members":'
        b'[{"common_stock":true,"sector":"IT","symbol":"005930"}],'
        b'"universe_code":"KOSPI200"}'
    )


def test_a_member_is_compared_by_all_of_its_fields() -> None:
    assert UniverseMember("A", True, "x") != UniverseMember("A", True, "y")
