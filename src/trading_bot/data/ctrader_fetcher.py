"""Historical candle fetcher for cTrader Open API.

Three things make cTrader's trendbar API non-obvious:

1. **Symbol names are not IDs.** Trendbar requests take a numeric `symbolId`,
   not "EURUSD". We resolve names once via ProtoOASymbolsListReq and cache.

2. **Prices are scaled integers.** All prices are stored as
   `int(actual_price * 100_000)`. To save bandwidth, only `low` is sent in
   full; `open`, `high`, `close` are non-negative deltas added to `low`.

3. **Timestamps are minutes.** Trendbar timestamps come as
   `utcTimestampInMinutes` (uint32). Request timestamps go in as
   millisecond unix epochs.

Pagination: max 14000 bars/request, rate-limited at 5 req/s.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAGetTrendbarsReq,
    ProtoOAGetTrendbarsRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod

from trading_bot.observability.logging import get_logger

log = get_logger(__name__)


# cTrader's hard cap on bars per ProtoOAGetTrendbarsReq.
MAX_BARS_PER_REQUEST = 14_000

# All FX prices are stored as integers = actual_price * 100_000.
PRICE_SCALE = 100_000


# Our granularity strings → (ProtoOATrendbarPeriod enum value, minutes per bar).
# Note cTrader's enum is sparser than OANDA's; sub-minute and irregular
# timeframes (S5, M2, H2, H3, H6, H8) are not supported by the API at all.
GRANULARITY_MAP: dict[str, tuple[int, int]] = {
    "M1":  (ProtoOATrendbarPeriod.M1, 1),
    "M2":  (ProtoOATrendbarPeriod.M2, 2),
    "M3":  (ProtoOATrendbarPeriod.M3, 3),
    "M4":  (ProtoOATrendbarPeriod.M4, 4),
    "M5":  (ProtoOATrendbarPeriod.M5, 5),
    "M10": (ProtoOATrendbarPeriod.M10, 10),
    "M15": (ProtoOATrendbarPeriod.M15, 15),
    "M30": (ProtoOATrendbarPeriod.M30, 30),
    "H1":  (ProtoOATrendbarPeriod.H1, 60),
    "H4":  (ProtoOATrendbarPeriod.H4, 240),
    "H12": (ProtoOATrendbarPeriod.H12, 720),
    "D1":  (ProtoOATrendbarPeriod.D1, 1440),
    "W1":  (ProtoOATrendbarPeriod.W1, 10_080),
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


class ProtocolLike(Protocol):
    """Subset of CTraderProtocol the fetcher needs. Lets tests pass a fake
    without instantiating the Twisted client."""

    @property
    def account_id(self) -> int: ...

    def send(self, message: Any) -> Any: ...


def _trendbar_to_candle(
    trendbar: Any,
    instrument: str,
    granularity: str,
) -> Candle:
    """Reconstruct OHLCV from cTrader's delta-encoded trendbar."""
    low = trendbar.low / PRICE_SCALE
    return Candle(
        instrument=instrument,
        granularity=granularity,
        ts=datetime.fromtimestamp(
            trendbar.utcTimestampInMinutes * 60, tz=timezone.utc
        ),
        open=(trendbar.low + trendbar.deltaOpen) / PRICE_SCALE,
        high=(trendbar.low + trendbar.deltaHigh) / PRICE_SCALE,
        low=low,
        close=(trendbar.low + trendbar.deltaClose) / PRICE_SCALE,
        volume=int(trendbar.volume),
    )


class CTraderFetcher:
    """Pulls historical trendbars from cTrader Open API into TimescaleDB."""

    def __init__(self, protocol: ProtocolLike) -> None:
        self._protocol = protocol
        self._symbol_cache: dict[str, int] = {}

    def _ensure_symbols_loaded(self) -> None:
        if self._symbol_cache:
            return
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self._protocol.account_id
        response = self._protocol.send(req)
        # Defensive: confirm we got the right response type.
        if not isinstance(response, ProtoOASymbolsListRes):
            raise RuntimeError(
                f"Expected ProtoOASymbolsListRes, got {type(response).__name__}"
            )
        for symbol in response.symbol:
            self._symbol_cache[symbol.symbolName] = symbol.symbolId
        log.info("ctrader_symbols_loaded", count=len(self._symbol_cache))

    def list_symbols(self) -> dict[str, int]:
        """Return the {name: id} mapping. Triggers the symbols-list request
        on first call."""
        self._ensure_symbols_loaded()
        return dict(self._symbol_cache)

    def resolve_symbol(self, instrument: str) -> int:
        """Map an instrument name (e.g. 'EURUSD') to its numeric symbolId."""
        self._ensure_symbols_loaded()
        try:
            return self._symbol_cache[instrument]
        except KeyError as e:
            raise ValueError(
                f"Unknown instrument {instrument!r}. "
                f"Run `tbot ctrader symbols` to list available names — broker "
                f"naming varies (e.g. 'EURUSD' vs 'EUR/USD')."
            ) from e

    def fetch(
        self,
        instrument: str,
        granularity: str,
        start: datetime,
        end: datetime | None = None,
    ) -> Iterator[Candle]:
        """Yield candles in [start, end) in time order, paginating.

        Generator so callers can stream into the DB without buffering the
        full history in memory.
        """
        if granularity not in GRANULARITY_MAP:
            raise ValueError(
                f"Unsupported granularity {granularity!r}. "
                f"Valid: {sorted(GRANULARITY_MAP)}"
            )
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")

        end = end or datetime.now(tz=timezone.utc)
        period_enum, minutes_per_bar = GRANULARITY_MAP[granularity]
        symbol_id = self.resolve_symbol(instrument)
        delta = timedelta(minutes=minutes_per_bar)

        # Each batch covers up to MAX_BARS × granularity worth of wall clock.
        window = timedelta(minutes=MAX_BARS_PER_REQUEST * minutes_per_bar)

        current_from = start
        total = 0
        batch_n = 0

        while current_from < end:
            batch_n += 1
            batch_to = min(current_from + window, end)

            req = ProtoOAGetTrendbarsReq()
            req.ctidTraderAccountId = self._protocol.account_id
            req.symbolId = symbol_id
            req.period = period_enum
            req.fromTimestamp = int(current_from.timestamp() * 1000)
            req.toTimestamp = int(batch_to.timestamp() * 1000)

            response = self._protocol.send(req)
            if not isinstance(response, ProtoOAGetTrendbarsRes):
                raise RuntimeError(
                    f"Expected ProtoOAGetTrendbarsRes, got {type(response).__name__}"
                )
            trendbars = response.trendbar

            yielded = 0
            last_ts: datetime | None = None
            reached_end = False
            for trendbar in trendbars:
                candle = _trendbar_to_candle(trendbar, instrument, granularity)
                if candle.ts >= end:
                    reached_end = True
                    break
                yield candle
                total += 1
                yielded += 1
                last_ts = candle.ts

            log.info(
                "ctrader_fetch_batch",
                instrument=instrument,
                granularity=granularity,
                batch=batch_n,
                raw=len(trendbars),
                yielded=yielded,
                cumulative=total,
            )

            if reached_end:
                break
            if not trendbars:
                break
            # Defensive: stop if we didn't advance — prevents infinite loops
            # if the API returns repeated stale bars.
            if last_ts is None or last_ts < current_from:
                break
            current_from = last_ts + delta

        log.info(
            "ctrader_fetch_complete",
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
        """Fetch and upsert into Postgres. Returns count. Idempotent."""
        from trading_bot.data.candles import upsert_candles

        return upsert_candles(self.fetch(instrument, granularity, start, end))
