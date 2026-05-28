"""Bollinger-band mean reversion — bets that price snaps back to its mean.

Rationale: EUR/USD (and FX majors generally) range rather than trend in the
modern regime, so fading extremes fits the data's character far better than
chasing breakouts. This is the opposite bet to Donchian.

Rules:
  - Bands: middle = SMA(period); upper/lower = middle ± num_std × stdev(period)
  - Long entry:  close < lower band   (oversold — bet on a bounce up)
  - Long exit:   close > middle band  (reverted to the mean)
  - Short entry: close > upper band   (overbought — bet on a drop)
  - Short exit:  close < middle band

Look-ahead safety: bands are computed from the prior `period` bars
(`.shift(1)`), then compared to the current close. No future data leaks in,
and the current (possibly extreme) bar doesn't widen its own band.

Tail-risk warning: mean reversion's failure mode is a market that DOESN'T
revert — a strong trend keeps hitting the band and you keep adding to a loser.
A stop-loss (run_backtest's sl_stop) is strongly recommended live.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trading_bot.strategies.base import SignalSet


@dataclass(frozen=True)
class BollingerParams:
    period: int = 20
    num_std: float = 2.0

    def __post_init__(self) -> None:
        if self.period < 2:
            raise ValueError("period must be >= 2")
        if self.num_std <= 0:
            raise ValueError("num_std must be > 0")


class MeanReversionStrategy:
    name = "meanrev"

    def __init__(self, params: BollingerParams | None = None) -> None:
        self.params = params or BollingerParams()

    def generate_signals(self, candles: pd.DataFrame) -> SignalSet:
        if "close" not in candles.columns:
            raise ValueError("candles missing column: close")

        close = candles["close"]
        n, k = self.params.period, self.params.num_std

        # Prior-N band bounds (shift(1) → no look-ahead, no self-referencing).
        middle = close.rolling(n).mean().shift(1)
        std = close.rolling(n).std().shift(1)
        upper = middle + k * std
        lower = middle - k * std

        long_entries = close < lower
        long_exits = close > middle
        short_entries = close > upper
        short_exits = close < middle

        return SignalSet(
            long_entries=long_entries.fillna(False).astype(bool),
            long_exits=long_exits.fillna(False).astype(bool),
            short_entries=short_entries.fillna(False).astype(bool),
            short_exits=short_exits.fillna(False).astype(bool),
        )
