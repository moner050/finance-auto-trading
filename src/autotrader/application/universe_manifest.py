"""The document that says what the strategy is allowed to look at.

Section 11.5 refuses a row editor. The reason is that a universe is not a
collection of independently true facts; it is one published list as of one
date, and adding a symbol to it by hand produces a universe nobody published.
So the unit here is a whole manifest, and there is no way to express less than
one.

Membership is point in time. "Was this a KOSPI 200 common share on the day
that trade was decided" is a different question from "is it one now", and only
the first one is answerable about the past. A snapshot therefore carries the
date it is true as of, and history is kept rather than overwritten.

The digest covers the membership and the date, and deliberately not the
provenance. Two operators fetching the same published list from different
mirrors should agree that it is the same universe; a changed symbol should
always disagree. Where the file came from is recorded beside the snapshot and
audited, but it is not part of what the universe is.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from autotrader.shared.time import require_utc

# The three authorities section 11.5 names. A code outside this set is not a
# universe this system knows how to be filtered by.
KOSPI200 = "KOSPI200"
SP100 = "SP100"
BINANCE_USDM = "BINANCE_USDM"
UNIVERSE_CODES = frozenset({KOSPI200, SP100, BINANCE_USDM})

# The equity authorities are published as constituent lists carrying both
# common and preferred lines, and the strategy filters on common shares. A
# perpetual future is neither, so the flag is required of one group and
# refused of the other rather than defaulted for both.
COMMON_SHARE_UNIVERSES = frozenset({KOSPI200, SP100})

_MAXIMUM_MEMBERS = 5000
_MAXIMUM_SYMBOL = 32
_MAXIMUM_SECTOR = 64
_MAXIMUM_SOURCE_NAME = 64
_MAXIMUM_SOURCE_REFERENCE = 255


class UniverseManifestError(ValueError):
    """Raised when an uploaded manifest is not an authority we can store."""


@dataclass(frozen=True, slots=True)
class UniverseMember:
    """One line of a published list."""

    symbol: str
    common_stock: bool | None
    sector: str | None


@dataclass(frozen=True, slots=True)
class UniverseProvenance:
    """Where the operator says the list came from."""

    name: str
    reference: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class UniverseManifest:
    """One published list, as of one date."""

    universe_code: str
    effective_date: date
    provenance: UniverseProvenance
    members: tuple[UniverseMember, ...]

    @property
    def content_digest(self) -> bytes:
        return hashlib.sha256(canonical_bytes(self)).digest()

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(member.symbol for member in self.members)


@dataclass(frozen=True, slots=True)
class UniverseDifference:
    """What changed between two snapshots of the same authority.

    Section 11.5 asks for a comparison before activation because the operator
    is about to change what the strategy may trade, and a member count is not
    enough to see that one symbol was swapped for another.
    """

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    @property
    def identical(self) -> bool:
        return not (self.added or self.removed or self.changed)


def parse_manifest(document: bytes | str) -> UniverseManifest:
    """Read an uploaded manifest, or refuse it by name.

    Every refusal here is preferable to the alternative, which is a universe
    that looks published and is not.
    """
    try:
        decoded = json.loads(document)
    except ValueError as error:
        raise UniverseManifestError("manifest is not JSON") from error
    if not isinstance(decoded, dict):
        raise UniverseManifestError("manifest must be an object")
    payload = cast("dict[str, object]", decoded)

    universe_code = _text(payload.get("universe_code"), "universe_code", 16)
    if universe_code not in UNIVERSE_CODES:
        raise UniverseManifestError(f"{universe_code} is not a known universe")
    return UniverseManifest(
        universe_code=universe_code,
        effective_date=_date(payload.get("effective_date")),
        provenance=_provenance(payload.get("source")),
        members=_members(payload.get("members"), universe_code=universe_code),
    )


def canonical_bytes(manifest: UniverseManifest) -> bytes:
    """The exact bytes the digest is taken over.

    Sorted and separator-pinned, so the same list uploaded twice - reordered,
    reindented, or round-tripped through a spreadsheet - digests the same, and
    a single changed symbol never does.
    """
    if type(manifest) is not UniverseManifest:
        raise TypeError("manifest must be an exact UniverseManifest")
    return json.dumps(
        {
            "universe_code": manifest.universe_code,
            "effective_date": manifest.effective_date.isoformat(),
            "members": [
                {
                    "symbol": member.symbol,
                    "common_stock": member.common_stock,
                    "sector": member.sector,
                }
                for member in sorted(manifest.members, key=lambda item: item.symbol)
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def verify_digest(manifest: UniverseManifest, *, claimed: bytes) -> None:
    """Refuse a manifest whose content is not what the operator says it is.

    The operator computes the digest from the file they were given and we
    compute it from the file we received. Agreement is the only evidence that
    those are the same file.
    """
    if type(claimed) is not bytes or len(claimed) != 32:
        raise UniverseManifestError("claimed digest must be 32 bytes")
    if manifest.content_digest != claimed:
        raise UniverseManifestError(
            "manifest content does not match the claimed digest"
        )


def compare(
    previous: UniverseManifest, current: UniverseManifest
) -> UniverseDifference:
    """What activating `current` would change, member by member."""
    for manifest in (previous, current):
        if type(manifest) is not UniverseManifest:
            raise TypeError("both sides must be an exact UniverseManifest")
    if previous.universe_code != current.universe_code:
        raise UniverseManifestError("only snapshots of one authority compare")
    before = {member.symbol: member for member in previous.members}
    after = {member.symbol: member for member in current.members}
    return UniverseDifference(
        added=tuple(sorted(after.keys() - before.keys())),
        removed=tuple(sorted(before.keys() - after.keys())),
        # A symbol that stayed but changed sector or share class is neither an
        # addition nor a removal, and calling it unchanged would hide the only
        # kind of edit a member count cannot see.
        changed=tuple(
            sorted(
                symbol
                for symbol in before.keys() & after.keys()
                if before[symbol] != after[symbol]
            )
        ),
    )


def _members(value: object, *, universe_code: str) -> tuple[UniverseMember, ...]:
    if not isinstance(value, list):
        raise UniverseManifestError("manifest members must be a list")
    entries = cast("list[object]", value)
    if not entries:
        raise UniverseManifestError("a universe with no members filters nothing")
    if len(entries) > _MAXIMUM_MEMBERS:
        raise UniverseManifestError("manifest has implausibly many members")
    members: list[UniverseMember] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise UniverseManifestError("each member must be an object")
        member = _member(cast("dict[str, object]", entry), universe_code=universe_code)
        if member.symbol in seen:
            raise UniverseManifestError(f"{member.symbol} appears twice")
        seen.add(member.symbol)
        members.append(member)
    return tuple(members)


def _member(entry: dict[str, object], *, universe_code: str) -> UniverseMember:
    symbol = _text(entry.get("symbol"), "symbol", _MAXIMUM_SYMBOL)
    common = entry.get("common_stock")
    if universe_code in COMMON_SHARE_UNIVERSES:
        if type(common) is not bool:
            raise UniverseManifestError(
                f"{symbol} needs an exact common_stock flag in {universe_code}"
            )
    elif common is not None:
        raise UniverseManifestError(
            f"{symbol} is not a share class, so common_stock does not apply"
        )
    sector = entry.get("sector")
    return UniverseMember(
        symbol=symbol,
        common_stock=common,
        # Absent rather than refused: the strategy already blocks with
        # SECTOR_AUTHORITY_UNAVAILABLE, which says more than a rejected upload.
        sector=None if sector is None else _text(sector, "sector", _MAXIMUM_SECTOR),
    )


def _provenance(value: object) -> UniverseProvenance:
    if not isinstance(value, dict):
        raise UniverseManifestError("manifest source must be an object")
    payload = cast("dict[str, object]", value)
    published = payload.get("published_at")
    if type(published) is not str:
        raise UniverseManifestError("source published_at must be a timestamp")
    try:
        moment = datetime.fromisoformat(published)
    except ValueError as error:
        raise UniverseManifestError("source published_at is not a timestamp") from error
    if moment.tzinfo is None:
        raise UniverseManifestError("source published_at must carry an offset")
    return UniverseProvenance(
        name=_text(payload.get("name"), "source name", _MAXIMUM_SOURCE_NAME),
        reference=_text(
            payload.get("reference"), "source reference", _MAXIMUM_SOURCE_REFERENCE
        ),
        published_at=require_utc(moment),
    )


def _date(value: object) -> date:
    if type(value) is not str:
        raise UniverseManifestError("effective_date must be a date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise UniverseManifestError("effective_date is not a date") from error


def _text(value: object, name: str, maximum: int) -> str:
    if type(value) is not str:
        raise UniverseManifestError(f"{name} must be text")
    stripped = value.strip()
    if not stripped or stripped != value:
        raise UniverseManifestError(f"{name} must be trimmed and non-empty")
    if len(stripped) > maximum:
        raise UniverseManifestError(f"{name} is longer than {maximum} characters")
    if any(character in stripped for character in "\r\n\t"):
        raise UniverseManifestError(f"{name} must be a single line")
    return stripped


__all__ = (
    "BINANCE_USDM",
    "COMMON_SHARE_UNIVERSES",
    "KOSPI200",
    "SP100",
    "UNIVERSE_CODES",
    "UniverseDifference",
    "UniverseManifest",
    "UniverseManifestError",
    "UniverseMember",
    "UniverseProvenance",
    "canonical_bytes",
    "compare",
    "parse_manifest",
    "verify_digest",
)
