"""Tests for ATR and volatility position sizing — the math that keeps risk
per trade constant across instruments of very different volatility."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot.risk.limits import RiskGate
from trading_bot.risk.sizing import atr, atr_stop_distance, volatility_position_size


def _candles(highs, lows, closes) -> pd.DataFrame:
    idx = pd.date_range("2022-01-01", periods=len(closes), freq="1D", tz="UTC")
    return pd.DataFrame({"high": highs, "low": lows, "close": closes}, index=idx)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


def test_atr_constant_range_equals_range() -> None:
    """If every bar has the same high-low range and no gaps, ATR == that range."""
    n = 20
    closes = [100.0] * n
    highs = [101.0] * n
    lows = [99.0] * n  # range = 2.0 each bar
    a = atr(_candles(highs, lows, closes), period=5)
    assert a.iloc[-1] == pytest.approx(2.0)


def test_atr_captures_gaps() -> None:
    """A gap up should make TR exceed the bar's own high-low range."""
    highs = [10, 10, 20, 20]
    lows = [9, 9, 19, 19]
    closes = [9.5, 9.5, 19.5, 19.5]  # gap from ~9.5 to ~19.5
    a = atr(_candles(highs, lows, closes), period=2)
    # The gap bar's TR = max(1, |20-9.5|, |19-9.5|) = 10.5, well above 1.0.
    assert a.iloc[2] > 1.0


def test_atr_validates_inputs() -> None:
    with pytest.raises(ValueError, match="period"):
        atr(_candles([1], [1], [1]), period=0)
    with pytest.raises(ValueError, match="missing"):
        atr(pd.DataFrame({"close": [1.0, 2.0]}), period=1)


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


def test_size_risks_exact_fraction() -> None:
    # 10,000 equity, 0.5% risk = $50 at risk. Stop 5.0 away, $1/point.
    # units = 50 / (5 * 1) = 10. A 5.0 adverse move loses 10*5*1 = $50. ✓
    units = volatility_position_size(10_000, 0.005, stop_distance=5.0, value_per_point=1.0)
    assert units == pytest.approx(10.0)
    assert units * 5.0 * 1.0 == pytest.approx(10_000 * 0.005)


def test_size_inversely_scales_with_volatility() -> None:
    """Wider stop (more volatile market) → fewer units, same money at risk."""
    calm = volatility_position_size(10_000, 0.005, stop_distance=2.0)
    wild = volatility_position_size(10_000, 0.005, stop_distance=8.0)
    assert wild < calm
    assert calm / wild == pytest.approx(4.0)  # 4x the stop → 1/4 the size


@pytest.mark.parametrize(
    "kwargs",
    [
        {"equity": 0, "risk_fraction": 0.01, "stop_distance": 1.0},
        {"equity": 1000, "risk_fraction": 0, "stop_distance": 1.0},
        {"equity": 1000, "risk_fraction": 0.01, "stop_distance": 0},
        {"equity": 1000, "risk_fraction": 2.0, "stop_distance": 1.0},
    ],
)
def test_size_validates_inputs(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        volatility_position_size(**kwargs)


def test_atr_stop_distance() -> None:
    assert atr_stop_distance(1.5, 2.0) == pytest.approx(3.0)
    with pytest.raises(ValueError):
        atr_stop_distance(0, 2.0)


# ---------------------------------------------------------------------------
# Gate integration
# ---------------------------------------------------------------------------


def test_gate_size_from_atr_returns_units_and_stop() -> None:
    gate = RiskGate()  # default max_account_risk_pct = 0.005
    units, stop = gate.size_from_atr(
        account_equity=1000, atr_value=10.0, atr_multiple=2.0, value_per_point=1.0
    )
    assert stop == pytest.approx(20.0)  # 2 × ATR
    # units = (1000 * 0.005) / (20 * 1) = 0.25
    assert units == pytest.approx(0.25)
