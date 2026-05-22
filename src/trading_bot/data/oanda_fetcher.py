"""Fetches historical candles from OANDA and writes them to TimescaleDB.

Used for backtesting and warming up indicators before live runs. The same
OANDA client works against practice or live — only the host differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trading_bot.config import get_settings
from trading_bot.observability.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Candle:
    instrument: str
    granularity: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class OandaFetcher:
    """Pulls candles from OANDA's REST API into the candles hypertable.

    This is a stub — week-1 milestone fills in the actual oandapyV20 calls,
    pagination over the 5000-candle limit per request, and the upsert into
    Postgres.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._token = settings.oanda_api_token.get_secret_value()
        self._account_id = settings.oanda_account_id
        self._base_url = settings.oanda_rest_url
        log.info(
            "oanda_fetcher_initialised",
            env=settings.oanda_env.value,
            base_url=self._base_url,
        )

    def fetch(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Fetch candles in [start, end). Returns them in time order.

        TODO(week-1): implement using oandapyV20.endpoints.instruments.InstrumentsCandles,
        page over the 5000-row limit, parse the bid/ask mid, dedupe overlap.
        """
        end = end or datetime.now(tz=timezone.utc)
        log.warning(
            "oanda_fetcher_stub",
            instrument=instrument,
            granularity=granularity,
            start=start.isoformat(),
            end=end.isoformat(),
            message="Stub — wire up oandapyV20 in week-1 milestone",
        )
        return []

    def backfill(self, instrument: str, granularity: str, start: datetime) -> int:
        """Fetch candles since `start` and upsert into Postgres. Returns row count.

        TODO(week-1): wire up the insert with ON CONFLICT DO UPDATE so re-runs
        are idempotent.
        """
        candles = self.fetch(instrument, granularity, start)
        log.info("backfill_complete", instrument=instrument, count=len(candles))
        return len(candles)
