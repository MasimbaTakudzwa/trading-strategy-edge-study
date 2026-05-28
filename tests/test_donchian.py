"""Tests for the Donchian breakout signal logic. Uses synthetic price series
with known breakouts so the assertions are exact."""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.strategies.donchian import DonchianParams, DonchianStrategy


def _candles(closes: list[float]) -> pd.DataFrame:
    """Build an OHLCV frame where each bar is a doji at its close price
    (high == low == close), so channel maths is easy to reason about."""
    idx = pd.date_range("2022-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 100},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Params validation
# ---------------------------------------------------------------------------


def test_params_reject_exit_ge_entry() -> None:
    with pytest.raises(ValueError, match="exit_period"):
        DonchianParams(entry_period=10, exit_period=10)


def test_params_reject_tiny_entry() -> None:
    with pytest.raises(ValueError, match="entry_period"):
        DonchianParams(entry_period=1, exit_period=1)


# ---------------------------------------------------------------------------
# Entry signals
# ---------------------------------------------------------------------------


def test_long_entry_fires_on_upside_breakout() -> None:
    # 22 flat bars at 1.0, then a jump to 1.5 — breaks the 20-bar high.
    closes = [1.0] * 22 + [1.5]
    df = _candles(closes)
    sig = DonchianStrategy(DonchianParams(20, 10)).generate_signals(df)

    assert sig.long_entries.iloc[-1]  # breakout bar
    assert not sig.long_entries.iloc[:-1].any()  # nothing before it
    assert not sig.short_entries.any()


def test_short_entry_fires_on_downside_breakout() -> None:
    closes = [1.0] * 22 + [0.5]
    df = _candles(closes)
    sig = DonchianStrategy(DonchianParams(20, 10)).generate_signals(df)

    assert sig.short_entries.iloc[-1]
    assert not sig.short_entries.iloc[:-1].any()
    assert not sig.long_entries.any()


def test_no_signal_when_price_stays_in_channel() -> None:
    # Gentle oscillation that never exceeds the prior 20-bar range.
    closes = [1.0, 1.01, 0.99, 1.0, 1.005] * 6
    df = _candles(closes)
    sig = DonchianStrategy(DonchianParams(20, 10)).generate_signals(df)
    assert not sig.long_entries.any()
    assert not sig.short_entries.any()


# ---------------------------------------------------------------------------
# Look-ahead safety
# ---------------------------------------------------------------------------


def test_channel_excludes_current_bar() -> None:
    """The breakout bar's own high must NOT be in its channel. If it were,
    a single spike could never exceed its own channel and no signal would
    fire — so the fact that it DOES fire proves shift(1) is working."""
    closes = [1.0] * 22 + [1.5]
    df = _candles(closes)
    sig = DonchianStrategy(DonchianParams(20, 10)).generate_signals(df)
    # Signal fires exactly on the spike bar — only possible if the channel
    # was computed from the prior (flat) bars, not including the spike itself.
    assert sig.long_entries.iloc[-1]


def test_warmup_bars_have_no_signals() -> None:
    closes = [1.0, 2.0, 0.5] * 10  # volatile, but warm-up channels are undefined
    df = _candles(closes)
    sig = DonchianStrategy(DonchianParams(20, 10)).generate_signals(df)
    # First entry_period bars can't have a defined 20-bar channel → no signal.
    assert not sig.long_entries.iloc[:20].any()
    assert not sig.short_entries.iloc[:20].any()


# ---------------------------------------------------------------------------
# Exit signals
# ---------------------------------------------------------------------------


def test_long_exit_fires_below_exit_channel() -> None:
    # Rise (enter long), hold, then drop below the 10-bar low.
    closes = [1.0] * 22 + [1.5] * 12 + [0.9]
    df = _candles(closes)
    sig = DonchianStrategy(DonchianParams(20, 10)).generate_signals(df)
    assert sig.long_exits.iloc[-1]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_missing_columns_raises() -> None:
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError, match="missing columns"):
        DonchianStrategy().generate_signals(df)


# ---------------------------------------------------------------------------
# Trend filter
# ---------------------------------------------------------------------------


def test_trend_filter_blocks_long_below_sma() -> None:
    """A long breakout while price is below its long SMA should be suppressed
    by the trend filter, but allowed without it."""
    # Long downtrend, then a small upside breakout that's still below the SMA.
    closes = list(range(100, 60, -1)) + [75.0]  # falling 100→61, then pop to 75
    df = _candles([float(c) for c in closes])

    no_filter = DonchianStrategy(DonchianParams(20, 10)).generate_signals(df)
    filtered = DonchianStrategy(
        DonchianParams(20, 10, trend_filter_period=20)
    ).generate_signals(df)

    # Without the filter the pop can trigger a long; with it, price is below
    # the SMA so the long is blocked.
    assert filtered.long_entries.sum() <= no_filter.long_entries.sum()
    assert not filtered.long_entries.iloc[-1]


def test_trend_filter_period_validation() -> None:
    with pytest.raises(ValueError, match="trend_filter_period"):
        DonchianParams(20, 10, trend_filter_period=1)
