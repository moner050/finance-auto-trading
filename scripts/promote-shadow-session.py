"""Claim and complete one Shadow promotion session for a past exchange date.

    python scripts/promote-shadow-session.py 2026-09-02

The same repository calls the back office route makes, in the same order:
claim records that a day was watched, complete recounts the evidence and
refuses if the manifest does not verify. Nothing here asserts a number - the
repository counts, and this passes it an id and a clock.

Safe to run twice. A day already claimed is claimed again idempotently by the
repository, and a day already complete refuses rather than reopening.

Why a script rather than the screen: the date only becomes claimable once it
is over, which for a UTC session means after midnight, and the operator is
usually not at the screen then. It changes nothing the screen would not - the
claim route asks for a login and a CSRF token and no second password, and the
session row records no actor, so there is no attribution to forge.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.apps.trader.binance_paper import BTCUSDT
from autotrader.apps.trader.startup import resolve_account
from autotrader.config.settings import Settings
from autotrader.execution.promotion.models import (
    PromotionMode,
    SessionStatus,
)
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.david_v6 import DavidV6ManifestRow
from autotrader.persistence.mysql.models.promotion import PromotionSessionRow
from autotrader.persistence.mysql.repositories.promotion import (
    PromotionRefusedError,
    PromotionSessions,
)
from autotrader.persistence.mysql.seeds.core import BINANCE_USDM_EXCHANGE_CODE
from autotrader.strategies.david_v6.models import V6Market

# The alias belongs to whoever runs this, not to the repository.
ACCOUNT_ALIAS = os.environ.get("AUTOTRADER_ACCOUNT_ALIAS", "")
USAGE = "usage: python scripts/promote-shadow-session.py <YYYY-MM-DD>"


async def run(exchange_date: date) -> int:
    engine = create_engine(Settings())
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        resolved = await resolve_account(
            sessions,
            account_alias=ACCOUNT_ALIAS,
            market=V6Market.BINANCE_USDM,
            instrument_code=BTCUSDT.code,
            exchange_code=BINANCE_USDM_EXCHANGE_CODE,
        )
        async with sessions() as store:
            repository = PromotionSessions(store)
            newest = await store.scalar(
                select(DavidV6ManifestRow).order_by(
                    DavidV6ManifestRow.registered_at.desc()
                )
            )
            if newest is None:
                print("no strategy manifest is registered", file=sys.stderr)
                return 1
            if newest.id != resolved.manifest_id:
                # Filing a day's evidence under a build that did not produce it
                # would make the promotion record say something untrue about
                # which code was being watched.
                print(
                    f"refused: the loop runs manifest {resolved.manifest_id} "
                    f"but the newest registered is {newest.id}",
                    file=sys.stderr,
                )
                return 1

            evidence = await repository.evidence_for(
                account_id=resolved.account.id, exchange_date=exchange_date
            )
            print(
                f"evidence for {exchange_date}: "
                f"decisions={evidence.decision_count} "
                f"orders={evidence.order_count} "
                f"incidents={evidence.blocking_incident_count} "
                f"recon={evidence.blocking_reconciliation_count} "
                f"unknown={evidence.unresolved_unknown_count}"
            )

            claimed = await repository.claim(
                binding_id=resolved.binding_id,
                account_id=resolved.account.id,
                manifest_id=newest.id,
                mode=PromotionMode.SHADOW,
                exchange_date=exchange_date,
                now=datetime.now(UTC),
            )
            await store.commit()
            print(f"claimed   {claimed.id} ({claimed.status.value})")

        async with sessions() as store:
            repository = PromotionSessions(store)
            row = await store.scalar(
                select(PromotionSessionRow).where(
                    PromotionSessionRow.mode == PromotionMode.SHADOW.value,
                    PromotionSessionRow.exchange_date == exchange_date,
                    PromotionSessionRow.status == SessionStatus.CLAIMED.value,
                )
            )
            if row is None:
                print(f"{exchange_date} is already complete")
                return 0
            moment = datetime.now(UTC)
            try:
                completed = await repository.complete(
                    session_id=row.id, now=moment, today=moment.date()
                )
            except PromotionRefusedError as error:
                # The repository counts for itself. A refusal here is the day
                # not being over, or evidence the manifest does not verify.
                await store.rollback()
                print(f"refused: {error}", file=sys.stderr)
                return 1
            await store.commit()
            print(f"completed {completed.id} at {completed.completed_at}")

        async with sessions() as store:
            state = await PromotionSessions(store).state(
                binding_id=resolved.binding_id, manifest_id=resolved.manifest_id
            )
        print(f"shadow complete : {state.shadow.completed_dates}")
        print(f"still required  : {state.shadow.remaining}")
        return 0
    finally:
        await engine.dispose()


def main(argv: tuple[str, ...]) -> int:
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        exchange_date = date.fromisoformat(argv[0])
    except ValueError:
        print(USAGE, file=sys.stderr)
        return 2
    return asyncio.run(run(exchange_date))


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
