"""Donchian channel breakout — a stripped-down Turtle-style trend system.

Rules (long; short is the mirror):
  - Entry: close breaks above the highest high of the prior `entry_period` bars
  - Exit:  close falls below the lowest low of the prior `exit_period` bars

The classic Turtle parameters are a 20-bar entry channel and a 10-bar exit
channel — wider entry to catch trends, tighter exit to give them back less.

Look-ahead safety: every channel is computed with `.shift(1)`, so the
breakout level at bar t uses only bars t-1 and earlier. We compare it against
bar t's close, which is known at bar close — no future information leaks in.

This module is the *vectorised* form used for backtesting. The event-driven
live form (Strategy.on_bar emitting Intents, see strategies/base.py) will
reuse the same channel maths on a trailing window — built when paper trading
is wired up (week 5).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class DonchianParams:
    entry_period: int = 20
    exit_period: int = 10
    # Optional long-term trend filter: only take longs when close is above its
    # `trend_filter_period`-bar simple moving average, shorts when below. None
    # disables it (raw breakout). This is the single biggest stability lever —
    # it keeps the system out of chop by only trading with the prevailing trend.
    trend_filter_period: int | None = None

    def __post_init__(self) -> None:
        if self.entry_period < 2:
            raise ValueError("entry_period must be >= 2")
        if self.exit_period < 1:
            raise ValueError("exit_period must be >= 1")
        if self.exit_period >= self.entry_period:
            # Not strictly illegal, but a tighter exit than entry is the point.
            raise ValueError("exit_period should be smaller than entry_period")
        if self.trend_filter_period is not None and self.trend_filter_period < 2:
            raise ValueError("trend_filter_period must be >= 2 or None")


@dataclass(frozen=True)
class SignalSet:
    """Boolean signal Series aligned to the candle index, for vectorbt."""

    long_entries: pd.Series
    long_exits: pd.Series
    short_entries: pd.Series
    short_exits: pd.Series


class DonchianStrategy:
    name = "donchian"

    def __init__(self, params: DonchianParams | None = None) -> None:
        self.params = params or DonchianParams()

    def generate_signals(self, candles: pd.DataFrame) -> SignalSet:
        """Compute breakout entry/exit signals from an OHLCV DataFrame.

        `candles` must have columns high/low/close and be sorted ascending
        by its time index.
        """
        required = {"high", "low", "close"}
        missing = required - set(candles.columns)
        if missing:
            raise ValueError(f"candles missing columns: {sorted(missing)}")

        high, low, close = candles["high"], candles["low"], candles["close"]
        ep, xp = self.params.entry_period, self.params.exit_period

        # Prior-N channel bounds (shift(1) excludes the current bar → no look-ahead).
        entry_upper = high.rolling(ep).max().shift(1)
        entry_lower = low.rolling(ep).min().shift(1)
        exit_lower = low.rolling(xp).min().shift(1)
        exit_upper = high.rolling(xp).max().shift(1)

        long_entries = close > entry_upper
        short_entries = close < entry_lower
        long_exits = close < exit_lower
        short_exits = close > exit_upper

        # Optional trend filter: gate entries by the side of the long SMA.
        # The SMA at bar t uses data through bar t's close (known at decision
        # time), so no look-ahead is introduced.
        if self.params.trend_filter_period is not None:
            sma = close.rolling(self.params.trend_filter_period).mean()
            long_entries = long_entries & (close > sma)
            short_entries = short_entries & (close < sma)

        # Warm-up bars (before the channels are defined) produce NaN → no signal.
        return SignalSet(
            long_entries=long_entries.fillna(False).astype(bool),
            long_exits=long_exits.fillna(False).astype(bool),
            short_entries=short_entries.fillna(False).astype(bool),
            short_exits=short_exits.fillna(False).astype(bool),
        )
