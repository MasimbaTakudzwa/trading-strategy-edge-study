"""Tests for the cTrader candle fetcher. The protocol layer is mocked with
a FakeProtocol that returns hand-built protobuf responses — tests cover the
fetcher's pagination, symbol resolution, price reconstruction, and defensive
bail-outs without touching the real Twisted/TLS stack."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAGetTrendbarsReq,
    ProtoOAGetTrendbarsRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod

from trading_bot.data.ctrader_fetcher import (
    GRANULARITY_MAP,
    MAX_BARS_PER_REQUEST,
    PRICE_SCALE,
    CTraderFetcher,
    _trendbar_to_candle,
)


# ---------------------------------------------------------------------------
# Helpers — build real protobuf objects so isinstance() in prod code passes.
# ---------------------------------------------------------------------------


ACCOUNT_ID = 12345


def make_symbols_res(mapping: dict[str, int]) -> ProtoOASymbolsListRes:
    res = ProtoOASymbolsListRes()
    res.ctidTraderAccountId = ACCOUNT_ID
    for name, sid in mapping.items():
        sym = res.symbol.add()
        sym.symbolId = sid
        sym.symbolName = name
        sym.enabled = True
    return res


def make_trendbar(
    ts: datetime,
    *,
    low: float = 1.0990,
    open_: float = 1.0995,
    high: float = 1.1010,
    close: float = 1.1000,
    volume: int = 100,
) -> dict[str, int]:
    """Compute the integer-encoded fields for one trendbar row."""
    low_i = int(round(low * PRICE_SCALE))
    return {
        "utcTimestampInMinutes": int(ts.timestamp() / 60),
        "low": low_i,
        "deltaOpen": int(round(open_ * PRICE_SCALE)) - low_i,
        "deltaHigh": int(round(high * PRICE_SCALE)) - low_i,
        "deltaClose": int(round(close * PRICE_SCALE)) - low_i,
        "volume": volume,
    }


def make_trendbars_res(bars: list[dict[str, int]]) -> ProtoOAGetTrendbarsRes:
    res = ProtoOAGetTrendbarsRes()
    res.ctidTraderAccountId = ACCOUNT_ID
    res.period = ProtoOATrendbarPeriod.H1
    res.symbolId = 1
    for b in bars:
        tb = res.trendbar.add()
        tb.utcTimestampInMinutes = b["utcTimestampInMinutes"]
        tb.low = b["low"]
        tb.deltaOpen = b["deltaOpen"]
        tb.deltaHigh = b["deltaHigh"]
        tb.deltaClose = b["deltaClose"]
        tb.volume = b["volume"]
    return res


class FakeProtocol:
    """Stand-in for CTraderProtocol — returns canned responses in order."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[Any] = []

    @property
    def account_id(self) -> int:
        return ACCOUNT_ID

    def send(self, message: Any) -> Any:
        self.requests.append(message)
        if not self._responses:
            raise AssertionError("FakeProtocol got an unexpected extra request")
        return self._responses.pop(0)


def _hourly_bars(start: datetime, n: int) -> list[dict[str, int]]:
    return [make_trendbar(start + timedelta(hours=i)) for i in range(n)]


# ---------------------------------------------------------------------------
# Price reconstruction
# ---------------------------------------------------------------------------


def test_trendbar_to_candle_reconstructs_ohlc() -> None:
    """Verify the low-plus-deltas decoding matches the original prices."""
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    raw = make_trendbar(ts, low=1.0990, open_=1.0995, high=1.1010, close=1.1000, volume=42)

    class TB:
        pass

    tb = TB()
    for k, v in raw.items():
        setattr(tb, k, v)

    candle = _trendbar_to_candle(tb, "EURUSD", "H1")
    assert candle.low == pytest.approx(1.0990)
    assert candle.open == pytest.approx(1.0995)
    assert candle.high == pytest.approx(1.1010)
    assert candle.close == pytest.approx(1.1000)
    assert candle.volume == 42
    assert candle.ts == ts


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------


def test_resolve_symbol_caches_after_first_lookup() -> None:
    """First call hits the API; second call returns from cache."""
    proto = FakeProtocol([make_symbols_res({"EURUSD": 1, "GBPUSD": 2})])
    fetcher = CTraderFetcher(proto)

    assert fetcher.resolve_symbol("EURUSD") == 1
    assert fetcher.resolve_symbol("GBPUSD") == 2
    # Only one network call, even with two resolve_symbol calls
    assert sum(isinstance(r, ProtoOASymbolsListReq) for r in proto.requests) == 1


def test_resolve_symbol_raises_for_unknown_name() -> None:
    proto = FakeProtocol([make_symbols_res({"EURUSD": 1})])
    fetcher = CTraderFetcher(proto)
    with pytest.raises(ValueError, match="Unknown instrument"):
        fetcher.resolve_symbol("XAUUSD")


def test_list_symbols_returns_copy() -> None:
    """Mutations to the returned dict don't affect the cache."""
    proto = FakeProtocol([make_symbols_res({"EURUSD": 1})])
    fetcher = CTraderFetcher(proto)
    symbols = fetcher.list_symbols()
    symbols.clear()
    # Cache is intact
    assert fetcher.resolve_symbol("EURUSD") == 1


# ---------------------------------------------------------------------------
# Granularity map
# ---------------------------------------------------------------------------


def test_granularity_map_covers_common_timeframes() -> None:
    for tf in ("M1", "M5", "M15", "M30", "H1", "H4", "D1"):
        assert tf in GRANULARITY_MAP
        period, minutes = GRANULARITY_MAP[tf]
        assert minutes > 0
        assert isinstance(period, int)  # protobuf enum values are ints


# ---------------------------------------------------------------------------
# Pagination + fetch behaviour
# ---------------------------------------------------------------------------


def test_fetch_returns_no_bars_when_response_empty() -> None:
    """Empty trendbar list → loop exits cleanly with zero yield."""
    proto = FakeProtocol(
        [
            make_symbols_res({"EURUSD": 1}),
            make_trendbars_res([]),
        ]
    )
    fetcher = CTraderFetcher(proto)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)
    assert list(fetcher.fetch("EURUSD", "H1", start, end)) == []


def test_fetch_stops_at_end_bound() -> None:
    """Bars at or after `end` are not yielded."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 3, 0, tzinfo=timezone.utc)
    bars = _hourly_bars(start, 10)  # 10 bars, spans well past `end`
    proto = FakeProtocol(
        [
            make_symbols_res({"EURUSD": 1}),
            make_trendbars_res(bars),
        ]
    )
    fetcher = CTraderFetcher(proto)
    candles = list(fetcher.fetch("EURUSD", "H1", start, end))
    # 00:00, 01:00, 02:00 — exclusive of 03:00 (== end)
    assert len(candles) == 3
    assert candles[-1].ts == datetime(2024, 1, 1, 2, 0, tzinfo=timezone.utc)


def test_fetch_paginates_across_multiple_windows() -> None:
    """When a batch fills the cap, fetcher advances `from` and requests again.

    Two contiguous full-coverage batches: the second one fills exactly up to
    `end`, so the loop exits without a third request.
    """
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=MAX_BARS_PER_REQUEST + 500)

    batch1 = _hourly_bars(start, MAX_BARS_PER_REQUEST)
    batch2_start = start + timedelta(hours=MAX_BARS_PER_REQUEST)
    batch2 = _hourly_bars(batch2_start, 500)  # covers exactly up to `end`

    proto = FakeProtocol(
        [
            make_symbols_res({"EURUSD": 1}),
            make_trendbars_res(batch1),
            make_trendbars_res(batch2),
        ]
    )
    fetcher = CTraderFetcher(proto)
    candles = list(fetcher.fetch("EURUSD", "H1", start, end))
    assert len(candles) == MAX_BARS_PER_REQUEST + 500

    trendbar_reqs = [r for r in proto.requests if isinstance(r, ProtoOAGetTrendbarsReq)]
    assert len(trendbar_reqs) == 2
    # Second request's fromTimestamp must be later than the first's
    assert trendbar_reqs[1].fromTimestamp > trendbar_reqs[0].fromTimestamp


def test_fetch_terminates_on_empty_followup_batch() -> None:
    """If the API has no more bars (gap, end of history), an empty response
    terminates the loop cleanly."""
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=MAX_BARS_PER_REQUEST + 500)

    batch1 = _hourly_bars(start, MAX_BARS_PER_REQUEST)
    proto = FakeProtocol(
        [
            make_symbols_res({"EURUSD": 1}),
            make_trendbars_res(batch1),
            make_trendbars_res([]),  # empty 2nd batch → stop
        ]
    )
    fetcher = CTraderFetcher(proto)
    candles = list(fetcher.fetch("EURUSD", "H1", start, end))
    assert len(candles) == MAX_BARS_PER_REQUEST


def test_fetch_request_carries_account_and_symbol_id() -> None:
    """The request must populate ctidTraderAccountId, symbolId, and period."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=5)
    bars = _hourly_bars(start, 5)
    proto = FakeProtocol(
        [
            make_symbols_res({"EURUSD": 7}),
            make_trendbars_res(bars),
        ]
    )
    fetcher = CTraderFetcher(proto)
    list(fetcher.fetch("EURUSD", "H1", start, end))

    trendbar_reqs = [r for r in proto.requests if isinstance(r, ProtoOAGetTrendbarsReq)]
    assert trendbar_reqs[0].ctidTraderAccountId == ACCOUNT_ID
    assert trendbar_reqs[0].symbolId == 7
    assert trendbar_reqs[0].period == ProtoOATrendbarPeriod.H1


def test_fetch_rejects_naive_start() -> None:
    proto = FakeProtocol([make_symbols_res({"EURUSD": 1})])
    fetcher = CTraderFetcher(proto)
    with pytest.raises(ValueError, match="timezone-aware"):
        list(fetcher.fetch("EURUSD", "H1", datetime(2024, 1, 1)))


def test_fetch_rejects_unknown_granularity() -> None:
    proto = FakeProtocol([])
    fetcher = CTraderFetcher(proto)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="granularity"):
        list(fetcher.fetch("EURUSD", "S5", start))


def test_fetch_uses_resolved_symbol_id_not_string() -> None:
    """Ensures the fetcher doesn't accidentally pass the instrument name to
    the API (which would be a silent bug — API expects numeric IDs)."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = _hourly_bars(start, 2)
    proto = FakeProtocol(
        [
            make_symbols_res({"EURUSD": 99}),
            make_trendbars_res(bars),
        ]
    )
    fetcher = CTraderFetcher(proto)
    list(fetcher.fetch("EURUSD", "H1", start, start + timedelta(hours=2)))

    trendbar_reqs = [r for r in proto.requests if isinstance(r, ProtoOAGetTrendbarsReq)]
    assert trendbar_reqs[0].symbolId == 99
