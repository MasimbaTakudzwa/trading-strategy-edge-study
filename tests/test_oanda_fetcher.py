"""Tests for the OANDA candle fetcher. The OANDA API is mocked — these
tests cover parsing, pagination, incomplete-candle filtering, and the
defensive bail-outs that prevent infinite loops."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.data.oanda_fetcher import (
    GRANULARITY_DELTAS,
    MAX_CANDLES_PER_REQUEST,
    OandaFetcher,
    _parse_candle,
    _parse_oanda_timestamp,
    _to_rfc3339_z,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_candle(ts: str, *, complete: bool = True, close: float = 1.1000) -> dict:
    """Build a fake OANDA candle dict."""
    return {
        "complete": complete,
        "time": ts,
        "volume": 100,
        "mid": {
            "o": "1.0990",
            "h": "1.1010",
            "l": "1.0985",
            "c": str(close),
        },
    }


class FakeAPI:
    """Mock oandapyV20.API. Returns canned batches in order."""

    def __init__(self, batches: list[list[dict]]) -> None:
        self._batches = list(batches)
        self.requests: list[dict] = []

    def request(self, endpoint) -> dict:  # type: ignore[no-untyped-def]
        self.requests.append(endpoint.params)
        batch = self._batches.pop(0) if self._batches else []
        return {"candles": batch}


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_oanda_timestamp_nanoseconds() -> None:
    """OANDA's default format includes nanoseconds — fromisoformat only
    handles microseconds, so we have to truncate."""
    parsed = _parse_oanda_timestamp("2024-01-01T00:00:00.000000000Z")
    assert parsed == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_parse_oanda_timestamp_microseconds() -> None:
    parsed = _parse_oanda_timestamp("2024-01-01T00:00:00.123456Z")
    assert parsed.microsecond == 123456


def test_parse_oanda_timestamp_no_fractional() -> None:
    parsed = _parse_oanda_timestamp("2024-01-01T00:00:00Z")
    assert parsed == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_to_rfc3339_z_converts_utc() -> None:
    dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert _to_rfc3339_z(dt) == "2024-01-01T00:00:00Z"


def test_to_rfc3339_z_converts_non_utc_tz() -> None:
    tz_plus_one = timezone(timedelta(hours=1))
    dt = datetime(2024, 1, 1, 1, 0, tzinfo=tz_plus_one)  # = 00:00 UTC
    assert _to_rfc3339_z(dt) == "2024-01-01T00:00:00Z"


def test_to_rfc3339_z_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _to_rfc3339_z(datetime(2024, 1, 1))


# ---------------------------------------------------------------------------
# Candle parsing
# ---------------------------------------------------------------------------


def test_parse_candle_skips_incomplete() -> None:
    raw = _raw_candle("2024-01-01T00:00:00Z", complete=False)
    assert _parse_candle(raw, "EUR_USD", "H1") is None


def test_parse_candle_extracts_mid_prices() -> None:
    raw = _raw_candle("2024-01-01T00:00:00Z", close=1.1234)
    candle = _parse_candle(raw, "EUR_USD", "H1")
    assert candle is not None
    assert candle.instrument == "EUR_USD"
    assert candle.granularity == "H1"
    assert candle.close == pytest.approx(1.1234)
    assert candle.ts == datetime(2024, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Granularity map
# ---------------------------------------------------------------------------


def test_granularity_map_covers_common_timeframes() -> None:
    for tf in ("M1", "M5", "M15", "M30", "H1", "H4", "D"):
        assert tf in GRANULARITY_DELTAS
        assert GRANULARITY_DELTAS[tf] > timedelta(0)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _hourly_batch(start: datetime, n: int, *, complete: bool = True) -> list[dict]:
    return [
        _raw_candle((start + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
                    complete=complete)
        for i in range(n)
    ]


def test_fetch_stops_when_batch_under_cap() -> None:
    """A batch smaller than the cap means we've reached the end of the data."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 2, 1, tzinfo=timezone.utc)
    batch = _hourly_batch(start, 100)
    api = FakeAPI([batch])

    fetcher = OandaFetcher(api=api)
    candles = list(fetcher.fetch("EUR_USD", "H1", start, end))

    assert len(candles) == 100
    assert len(api.requests) == 1


def test_fetch_paginates_across_multiple_full_batches() -> None:
    """Multiple full-size batches should produce one request each, then a
    smaller batch terminates."""
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, tzinfo=timezone.utc)

    batch1 = _hourly_batch(start, MAX_CANDLES_PER_REQUEST)
    next_start = start + timedelta(hours=MAX_CANDLES_PER_REQUEST)
    batch2 = _hourly_batch(next_start, MAX_CANDLES_PER_REQUEST)
    next_start_2 = next_start + timedelta(hours=MAX_CANDLES_PER_REQUEST)
    batch3 = _hourly_batch(next_start_2, 250)  # smaller → stop after this
    api = FakeAPI([batch1, batch2, batch3])

    fetcher = OandaFetcher(api=api)
    candles = list(fetcher.fetch("EUR_USD", "H1", start, end))

    assert len(candles) == MAX_CANDLES_PER_REQUEST * 2 + 250
    assert len(api.requests) == 3
    # Each `from` should advance past the previous batch's last timestamp.
    froms = [r["from"] for r in api.requests]
    assert froms[0] != froms[1] != froms[2]


def test_fetch_filters_incomplete_candles() -> None:
    """Incomplete bars are skipped and not yielded."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)
    # 5 complete + 1 incomplete (still forming)
    batch = _hourly_batch(start, 5) + [
        _raw_candle("2024-01-01T05:00:00Z", complete=False)
    ]
    api = FakeAPI([batch])

    fetcher = OandaFetcher(api=api)
    candles = list(fetcher.fetch("EUR_USD", "H1", start, end))

    assert len(candles) == 5
    assert all(c.ts >= start for c in candles)


def test_fetch_stops_at_end_bound() -> None:
    """Candles at or after `end` are not yielded, and the loop stops."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 3, 0, tzinfo=timezone.utc)  # exclusive upper bound
    batch = _hourly_batch(start, 10)  # 10 candles spanning past `end`
    api = FakeAPI([batch])

    fetcher = OandaFetcher(api=api)
    candles = list(fetcher.fetch("EUR_USD", "H1", start, end))

    # Should yield 00:00, 01:00, 02:00 — but not 03:00 (== end) or later.
    assert len(candles) == 3
    assert candles[-1].ts == datetime(2024, 1, 1, 2, 0, tzinfo=timezone.utc)


def test_fetch_handles_empty_response() -> None:
    """No candles → loop exits cleanly."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 2, 1, tzinfo=timezone.utc)
    api = FakeAPI([[]])

    fetcher = OandaFetcher(api=api)
    candles = list(fetcher.fetch("EUR_USD", "H1", start, end))

    assert candles == []


def test_fetch_rejects_naive_start() -> None:
    start = datetime(2024, 1, 1)  # no tz
    api = FakeAPI([])
    fetcher = OandaFetcher(api=api)
    with pytest.raises(ValueError, match="timezone-aware"):
        list(fetcher.fetch("EUR_USD", "H1", start))


def test_fetch_rejects_unknown_granularity() -> None:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    api = FakeAPI([])
    fetcher = OandaFetcher(api=api)
    with pytest.raises(ValueError, match="granularity"):
        list(fetcher.fetch("EUR_USD", "BOGUS", start))


def test_fetch_sends_mid_price_param() -> None:
    """Confirm we ask for mid prices, not bid or ask."""
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)
    api = FakeAPI([_hourly_batch(start, 5)])
    fetcher = OandaFetcher(api=api)
    list(fetcher.fetch("EUR_USD", "H1", start, end))
    assert api.requests[0]["price"] == "M"
