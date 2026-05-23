"""Fetches historical candles from OANDA and writes them to TimescaleDB.

Used for backtesting and warming up indicators before live runs. The same
client class works against practice or live — the environment is decided
by config and passed to oandapyV20.API as 'practice' or 'live'.

OANDA returns at most 5000 candles per request. To backfill long histories
we step `from` forward in time after each batch, using the last received
candle's timestamp plus one granularity tick as the next `from`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from oandapyV20 import API
from oandapyV20.endpoints.instruments import InstrumentsCandles
from oandapyV20.exceptions import V20Error
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from trading_bot.config import get_settings
from trading_bot.observability.logging import get_logger

log = get_logger(__name__)

# OANDA's hard cap on candles per request.
MAX_CANDLES_PER_REQUEST = 5000

# Granularity strings → fixed timedeltas. Used to advance `from` between
# pagination batches. The OANDA "M" (monthly) granularity is intentionally
# omitted — months aren't a fixed delta and we don't trade on that timeframe.
GRANULARITY_DELTAS: dict[str, timedelta] = {
    "S5": timedelta(seconds=5),
    "S10": timedelta(seconds=10),
    "S15": timedelta(seconds=15),
    "S30": timedelta(seconds=30),
    "M1": timedelta(minutes=1),
    "M2": timedelta(minutes=2),
    "M4": timedelta(minutes=4),
    "M5": timedelta(minutes=5),
    "M10": timedelta(minutes=10),
    "M15": timedelta(minutes=15),
    "M30": timedelta(minutes=30),
    "H1": timedelta(hours=1),
    "H2": timedelta(hours=2),
    "H3": timedelta(hours=3),
    "H4": timedelta(hours=4),
    "H6": timedelta(hours=6),
    "H8": timedelta(hours=8),
    "H12": timedelta(hours=12),
    "D": timedelta(days=1),
    "W": timedelta(weeks=1),
}


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


def _parse_oanda_timestamp(s: str) -> datetime:
    """OANDA returns RFC3339 with nanosecond precision (e.g.
    '2024-01-01T00:00:00.000000000Z'). Python's fromisoformat handles up to
    microseconds, so trim the extra precision and normalise the 'Z' suffix.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, _, tail = s.partition(".")
        # tz starts at the last + or - in the fractional+tz section
        sign_idx = max(tail.rfind("+"), tail.rfind("-"))
        if sign_idx == -1:
            frac, tz = tail, ""
        else:
            frac, tz = tail[:sign_idx], tail[sign_idx:]
        frac = frac[:6]  # truncate ns → us
        s = f"{head}.{frac}{tz}"
    return datetime.fromisoformat(s)


def _parse_candle(raw: dict, instrument: str, granularity: str) -> Candle | None:
    """Convert one OANDA candle dict to our Candle. Returns None for
    incomplete (still-forming) bars — we never persist those."""
    if not raw.get("complete", False):
        return None
    mid = raw["mid"]
    return Candle(
        instrument=instrument,
        granularity=granularity,
        ts=_parse_oanda_timestamp(raw["time"]),
        open=float(mid["o"]),
        high=float(mid["h"]),
        low=float(mid["l"]),
        close=float(mid["c"]),
        volume=int(raw.get("volume", 0)),
    )


def _to_rfc3339_z(dt: datetime) -> str:
    """OANDA prefers 'Z' over '+00:00' for UTC. Python's isoformat emits the
    latter, so swap it."""
    if dt.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware (use UTC)")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class OandaFetcher:
    """Pulls historical candles from OANDA's REST API into TimescaleDB."""

    def __init__(self, api: API | None = None) -> None:
        settings = get_settings()
        self._token = settings.oanda_api_token.get_secret_value()
        self._account_id = settings.oanda_account_id
        self._env = settings.oanda_env.value
        # oandapyV20 maps environment="practice"|"live" to the right host.
        self._api = api or API(access_token=self._token, environment=self._env)
        log.info("oanda_fetcher_initialised", env=self._env, account=self._account_id)

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type((V20Error, OSError)),
        reraise=True,
    )
    def _request_batch(
        self,
        instrument: str,
        granularity: str,
        from_ts: datetime,
        count: int = MAX_CANDLES_PER_REQUEST,
    ) -> list[dict]:
        params = {
            "granularity": granularity,
            "from": _to_rfc3339_z(from_ts),
            "count": count,
            "price": "M",  # mid prices
        }
        endpoint = InstrumentsCandles(instrument=instrument, params=params)
        response = self._api.request(endpoint)
        return response.get("candles", [])

    def fetch(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime | None = None,
    ) -> Iterator[Candle]:
        """Yield complete candles in [start, end) in time order, paginating.

        Generator so callers can stream straight into the DB without buffering
        the whole history in memory.
        """
        if granularity not in GRANULARITY_DELTAS:
            raise ValueError(f"Unsupported granularity: {granularity!r}")
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")

        end = end or datetime.now(tz=timezone.utc)
        delta = GRANULARITY_DELTAS[granularity]
        current_from = start
        total = 0
        batch_n = 0

        while current_from < end:
            batch_n += 1
            raw_candles = self._request_batch(instrument, granularity, current_from)
            if not raw_candles:
                log.info(
                    "fetch_batch_empty",
                    instrument=instrument,
                    granularity=granularity,
                    batch=batch_n,
                )
                break

            last_ts: datetime | None = None
            yielded_in_batch = 0
            reached_end = False

            for raw in raw_candles:
                candle = _parse_candle(raw, instrument, granularity)
                if candle is None:
                    continue
                if candle.ts >= end:
                    reached_end = True
                    break
                yield candle
                total += 1
                yielded_in_batch += 1
                last_ts = candle.ts

            log.info(
                "fetch_batch",
                instrument=instrument,
                batch=batch_n,
                raw=len(raw_candles),
                yielded=yielded_in_batch,
                cumulative=total,
            )

            if reached_end:
                break
            # Defensive: if nothing was yielded (all incomplete or invalid),
            # stop to avoid an infinite loop.
            if last_ts is None or last_ts <= current_from:
                break
            current_from = last_ts + delta
            # If the batch was smaller than the cap we're at the end of available data.
            if len(raw_candles) < MAX_CANDLES_PER_REQUEST:
                break

        log.info(
            "fetch_complete",
            instrument=instrument,
            granularity=granularity,
            total=total,
        )

    def backfill(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime | None = None,
    ) -> int:
        """Fetch candles since `start` and upsert into Postgres. Returns count.

        Idempotent on (instrument, granularity, ts) — safe to re-run over the
        same window.
        """
        # Local import to avoid a cycle (candles.py imports Candle from here).
        from trading_bot.data.candles import upsert_candles

        return upsert_candles(self.fetch(instrument, granularity, start, end))
