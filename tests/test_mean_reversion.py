"""Tests for the Bollinger mean-reversion signal logic, on synthetic series
with a known band and deliberate extreme moves."""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.strategies.mean_reversion import BollingerParams, MeanReversionStrategy


def _candles(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2022-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 100},
        index=idx,
    )


# 21 bars oscillating around 100 (mean ~100, std ~0.8) → a defined band,
# then we append an extreme bar.
_BASE = ([100.0, 101.0, 99.0] * 7)[:21]


def test_params_validation() -> None:
    with pytest.raises(ValueError, match="period"):
        BollingerParams(period=1)
    with pytest.raises(ValueError, match="num_std"):
        BollingerParams(num_std=0)


def test_long_entry_below_lower_band() -> None:
    df = _candles(_BASE + [90.0])  # well below lower band (~98)
    sig = MeanReversionStrategy(BollingerParams(20, 2.0)).generate_signals(df)
    assert sig.long_entries.iloc[-1]
    assert not sig.short_entries.iloc[-1]


def test_short_entry_above_upper_band() -> None:
    df = _candles(_BASE + [110.0])  # well above upper band (~102)
    sig = MeanReversionStrategy(BollingerParams(20, 2.0)).generate_signals(df)
    assert sig.short_entries.iloc[-1]
    assert not sig.long_entries.iloc[-1]


def test_long_exit_when_back_above_mean() -> None:
    df = _candles(_BASE + [90.0, 102.0])  # drop, then recover past the mean
    sig = MeanReversionStrategy(BollingerParams(20, 2.0)).generate_signals(df)
    assert sig.long_exits.iloc[-1]


def test_no_signal_inside_band() -> None:
    df = _candles(_BASE + [100.5])  # comfortably inside the band
    sig = MeanReversionStrategy(BollingerParams(20, 2.0)).generate_signals(df)
    assert not sig.long_entries.iloc[-1]
    assert not sig.short_entries.iloc[-1]


def test_warmup_has_no_signals() -> None:
    df = _candles([100.0, 90.0, 110.0] * 4)  # volatile but warm-up undefined
    sig = MeanReversionStrategy(BollingerParams(20, 2.0)).generate_signals(df)
    assert not sig.long_entries.iloc[:20].any()
    assert not sig.short_entries.iloc[:20].any()


def test_missing_close_raises() -> None:
    with pytest.raises(ValueError, match="close"):
        MeanReversionStrategy().generate_signals(pd.DataFrame({"high": [1.0, 2.0, 3.0]}))
