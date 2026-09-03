"""Is the tape deep enough to measure the thirty-second execution scale yet?

The plan's section 26 parked that measurement behind a condition, and a
condition in a document is a condition somebody has to remember. This answers
it from the tape instead, so a scheduled check can ask every week and say
"not yet" until it is.

What the threshold is, and why it is a number of setups rather than a date:

Milestone A in §26.2 is the entry rate at thirty seconds - what share of
five-minute setups complete an exhaustion chain there. At one minute it was
2.6%; §26.4 predicts 6-12%. Estimating a rate near 9% to within a standard
error of one point needs about 820 observations, and the two-year run at
`--pivot-left 24` reached the entry stage 12,837 times in 730 days, which is
17.6 a day. That is 47 days of tape, and 60 is taken as the threshold so the
estimate has room rather than only just arriving.

Nothing here runs the measurement or decides anything. It reports how deep
the tape is and whether that clears the bar.

    python scripts/thirty-second-readiness.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine

# §26.2's milestone A, in days of tape. See the module docstring for how it
# was derived; it is a sample size wearing a calendar's clothes.
REQUIRED_DAYS = 60
SETUPS_PER_DAY = 17.6


async def main() -> None:
    engine = create_engine(Settings())
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT symbol, COUNT(*) n, MIN(occurred_at) lo, "
                        "MAX(occurred_at) hi FROM market_binance_usdm_trade "
                        "GROUP BY symbol ORDER BY symbol"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    if not rows:
        print("READY=0 테이프가 비어 있다")
        return

    now = datetime.now(UTC)
    ready = False
    for row in rows:
        low = row.lo.replace(tzinfo=UTC)
        high = row.hi.replace(tzinfo=UTC)
        days = (high - low) / timedelta(days=1)
        stale = (now - high) / timedelta(minutes=1)
        setups = int(days * SETUPS_PER_DAY)
        print(
            f"{row.symbol}  {row.n:>12,}건  {days:>5.1f}일  "
            f"(예상 셋업 {setups:,})  최신 {stale:.0f}분 전"
        )
        if row.symbol == "BTCUSDT" and days >= REQUIRED_DAYS:
            ready = True

    remaining = max(
        0.0,
        REQUIRED_DAYS
        - max(
            (row.hi.replace(tzinfo=UTC) - row.lo.replace(tzinfo=UTC))
            / timedelta(days=1)
            for row in rows
            if row.symbol == "BTCUSDT"
        ),
    )
    print(f"기준 {REQUIRED_DAYS}일 (§26.2 이정표 A)")
    if ready:
        print("READY=1 측정을 돌릴 수 있다")
    else:
        when = (now + timedelta(days=remaining)).strftime("%Y-%m-%d")
        print(f"READY=0 {remaining:.0f}일 더 필요 (이 속도면 {when}쯤)")


asyncio.run(main())
