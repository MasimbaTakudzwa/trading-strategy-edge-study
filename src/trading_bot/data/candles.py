"""Candle persistence and read helpers.

Inserts are idempotent via ON CONFLICT DO UPDATE on the natural key
(instrument, granularity, ts). Re-running a fetch over the same window
won't duplicate rows; if the source data has been revised (rare for FX),
the row updates in place.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from trading_bot.data.ctrader_fetcher import Candle
from trading_bot.data.db import session_scope
from trading_bot.data.models import candles
from trading_bot.observability.logging import get_logger

log = get_logger(__name__)

# Chunk size for batch upserts — keeps individual statements small enough
# to stay well under Postgres parameter limits and bounded in memory.
DEFAULT_BATCH_SIZE = 1000


def _flush(session: Session, batch: list[dict]) -> None:
    if not batch:
        return
    stmt = pg_insert(candles).values(batch)
    stmt = stmt.on_conflict_do_update(
        index_elements=["instrument", "granularity", "ts"],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
        },
    )
    session.execute(stmt)


def upsert_candles(rows: Iterable[Candle], batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """Stream Candle objects into the candles table, batching writes.

    Returns the number of rows written.
    """
    total = 0
    batch: list[dict] = []

    with session_scope() as session:
        for c in rows:
            batch.append(
                {
                    "instrument": c.instrument,
                    "granularity": c.granularity,
                    "ts": c.ts,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
            )
            if len(batch) >= batch_size:
                _flush(session, batch)
                total += len(batch)
                batch = []
        if batch:
            _flush(session, batch)
            total += len(batch)

    log.info("upsert_candles_complete", total=total)
    return total


@dataclass(frozen=True)
class CandleRange:
    """Summary of stored candles for one (instrument, granularity)."""

    instrument: str
    granularity: str
    count: int
    earliest: datetime | None
    latest: datetime | None


def list_candle_ranges() -> list[CandleRange]:
    """Return a row per stored (instrument, granularity) with count and bounds.

    Powers `tbot db status` — quick read of what data is loaded.
    """
    stmt = (
        select(
            candles.c.instrument,
            candles.c.granularity,
            func.count().label("count"),
            func.min(candles.c.ts).label("earliest"),
            func.max(candles.c.ts).label("latest"),
        )
        .group_by(candles.c.instrument, candles.c.granularity)
        .order_by(candles.c.instrument, candles.c.granularity)
    )
    with session_scope() as session:
        result = session.execute(stmt)
        return [
            CandleRange(
                instrument=row.instrument,
                granularity=row.granularity,
                count=row.count,
                earliest=row.earliest,
                latest=row.latest,
            )
            for row in result
        ]


def latest_candle_ts(instrument: str, granularity: str) -> datetime | None:
    """Most recent stored timestamp for the given series, or None if empty.

    Used to decide where to resume an incremental backfill — `fetch from
    latest_candle_ts + delta` avoids re-pulling history we already have.
    """
    stmt = (
        select(func.max(candles.c.ts))
        .where(candles.c.instrument == instrument)
        .where(candles.c.granularity == granularity)
    )
    with session_scope() as session:
        return session.execute(stmt).scalar_one_or_none()
