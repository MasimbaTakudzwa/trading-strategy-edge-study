"""Position sizing and volatility helpers.

Core principle: every trade risks a constant fraction of equity. Given a stop
distance (how far price moves against us before we're stopped out) and the
money one unit gains/loses per unit of price move, the size that risks exactly
`equity * risk_fraction` is:

    units = (equity * risk_fraction) / (stop_distance * value_per_point)

The stop distance is volatility-scaled via ATR, so the risk budget adapts to
each market's noise: a calm market gets a tighter stop (more units), a wild
one a wider stop (fewer units) — equal money at risk either way. This is what
lets one strategy trade gold, an index, and FX with sane size on each.

`value_per_point` (money per 1.0 of price move per unit held) is instrument-
specific and comes from broker symbol specs; it defaults to 1.0 here (correct
for typical gold/index CFDs) and is sourced precisely in the execution layer.
"""

from __future__ import annotations

import pandas as pd


def atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range over `period` bars (simple-moving-average form).

    True Range = max(high-low, |high-prev_close|, |low-prev_close|), which
    captures gaps that a plain high-low range misses.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    required = {"high", "low", "close"}
    missing = required - set(candles.columns)
    if missing:
        raise ValueError(f"candles missing columns: {sorted(missing)}")

    high, low, close = candles["high"], candles["low"], candles["close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period).mean()


def atr_stop_distance(atr_value: float, atr_multiple: float = 2.0) -> float:
    """Stop distance in price terms = ATR × multiple. 2× ATR is a common,
    not-too-tight default that survives normal noise."""
    if atr_value <= 0:
        raise ValueError("atr_value must be > 0")
    if atr_multiple <= 0:
        raise ValueError("atr_multiple must be > 0")
    return atr_value * atr_multiple


def volatility_position_size(
    equity: float,
    risk_fraction: float,
    stop_distance: float,
    value_per_point: float = 1.0,
) -> float:
    """Units that risk exactly `equity * risk_fraction` if price moves
    `stop_distance` against the position.

    Returns a non-negative float (fractional units allowed; the execution
    layer rounds to the instrument's minimum step).
    """
    if equity <= 0:
        raise ValueError("equity must be > 0")
    if not 0 < risk_fraction <= 1:
        raise ValueError("risk_fraction must be in (0, 1]")
    if stop_distance <= 0:
        raise ValueError("stop_distance must be > 0")
    if value_per_point <= 0:
        raise ValueError("value_per_point must be > 0")

    risk_amount = equity * risk_fraction
    return risk_amount / (stop_distance * value_per_point)
