"""The executable v6 strategy, and the build a decision is recorded under.

Three rows the loop refuses to start without, and none of them had an
operational producer: a definition, a version, and the manifest naming the
exact source, design and configuration a decision was taken by. The paper
harness wrote them and nothing else did, so `--check` on a real database
answered "no strategy manifest is registered" with no way to fix it.

They are derived rather than chosen. The code, the version string and the
three hashes all come from `manifest.py`, and the repository re-checks every
one of them before it accepts the row: a definition whose configuration hash
disagrees is refused rather than stored. Change the strategy source or an
operator override and the hash moves, which is the point - decisions taken
under the old build stay attached to it.

`SHADOW` is where a newly registered version starts. It is the only status
besides LIVE_APPROVED the manifest authority admits, and promotion to LIVE is
section 11.8's job, behind two Shadow and two Paper sessions.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.david_v6 import DavidV6ManifestRow
from autotrader.persistence.mysql.models.strategy import (
    StrategyDefinition,
    StrategyVersion,
)
from autotrader.persistence.mysql.repositories.david_v6 import DavidV6Repository
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.manifest import (
    STRATEGY_CODE,
    STRATEGY_VERSION,
    V6_DESIGN_SHA256,
    V6_SOURCE_SHA256,
    V6Manifest,
    v6_configuration_hash,
)

DAVID_V6_DEFINITION_ID = UUID("019d0000-0000-7000-8000-000000000010")
DAVID_V6_VERSION_ID = UUID("019d0000-0000-7000-8000-000000000011")

SHADOW = "SHADOW"


async def register_david_v6_build(session: AsyncSession, *, now: datetime) -> UUID:
    """Ensure the definition, the version and this build's manifest exist.

    Returns the manifest id. Idempotent by content: the repository matches an
    existing manifest on strategy version, source hash and configuration hash,
    and returns it rather than writing a second row for the same build.
    """
    # The manifest records the build at whole-second resolution, and a real
    # clock does not produce one. Truncated rather than rounded: a build is
    # registered at the second it was registered in, never the next one.
    moment = require_utc(now).replace(microsecond=0)
    configuration_hash = v6_configuration_hash()

    definition = await session.scalar(
        select(StrategyDefinition).where(
            StrategyDefinition.id == DAVID_V6_DEFINITION_ID
        )
    )
    if definition is None:
        definition = StrategyDefinition(
            id=DAVID_V6_DEFINITION_ID,
            code=STRATEGY_CODE,
            research_only=False,
            configuration_hash=configuration_hash,
        )
        session.add(definition)
        await session.flush()
    elif definition.configuration_hash != configuration_hash:
        # Not corrected here. A definition that disagrees with the build means
        # the configuration moved, and silently rewriting it would detach
        # every decision recorded under the old hash from what produced it.
        raise ValueError(
            "the stored v6 definition was registered under a different "
            "configuration hash; register a new definition rather than "
            "overwriting this one"
        )

    version = await session.scalar(
        select(StrategyVersion).where(StrategyVersion.id == DAVID_V6_VERSION_ID)
    )
    if version is None:
        version = StrategyVersion(
            id=DAVID_V6_VERSION_ID,
            definition_id=DAVID_V6_DEFINITION_ID,
            version=STRATEGY_VERSION,
            status=SHADOW,
            research_only=False,
        )
        session.add(version)
        await session.flush()

    # Look before writing. A manifest carries the instant it was registered
    # at, so constructing a fresh one each run would present the same build
    # with a different id and a later timestamp - which the repository
    # correctly refuses as evidence that disagrees with what it holds.
    existing = await session.scalar(
        select(DavidV6ManifestRow).where(
            DavidV6ManifestRow.strategy_version_id == DAVID_V6_VERSION_ID,
            DavidV6ManifestRow.source_sha256 == V6_SOURCE_SHA256,
            DavidV6ManifestRow.configuration_hash == configuration_hash,
        )
    )
    if existing is not None:
        return existing.id

    manifest = V6Manifest(
        id=new_uuid7(),
        strategy_version_id=DAVID_V6_VERSION_ID,
        source_sha256=V6_SOURCE_SHA256,
        design_sha256=V6_DESIGN_SHA256,
        configuration_hash=configuration_hash,
        registered_at=moment,
    )
    stored = await DavidV6Repository(session).persist_manifest(manifest)
    return stored.id


__all__ = (
    "DAVID_V6_DEFINITION_ID",
    "DAVID_V6_VERSION_ID",
    "SHADOW",
    "register_david_v6_build",
)
