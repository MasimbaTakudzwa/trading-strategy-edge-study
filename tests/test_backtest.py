"""Tests for the vectorbt backtest runner. Synthetic trending data exercises
the full vectorbt integration without needing a database or network."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import vectorbt as vbt

from trading_bot.backtest.runner import freq_for, run_backtest
from trading_bot.strategies.donchian import DonchianParams, DonchianStrategy


def _trending_candles(n_up: int = 60, n_down: int = 60) -> pd.DataFrame:
    """A clean up-trend then down-trend — guarantees Donchian breakouts in
    both directions, so at least one round-trip trade occurs."""
    up = np.linspace(1.0, 1.5, n_up)
    down = np.linspace(1.5, 1.0, n_down)
    close = np.concatenate([up, down])
    idx = pd.date_range("2022-01-01", periods=len(close), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 100},
        index=idx,
    )


def test_freq_mapping() -> None:
    assert freq_for("H1") == "1h"
    assert freq_for("D1") == "1d"
    with pytest.raises(ValueError):
        freq_for("BOGUS")


def test_run_backtest_produces_trades_on_trend() -> None:
    df = _trending_candles()
    strat = DonchianStrategy(DonchianParams(entry_period=20, exit_period=10))
    result = run_backtest(df, strat, init_cash=1000, granularity="H1")

    assert isinstance(result.portfolio, vbt.Portfolio)
    assert "Total Trades" in result.stats.index
    assert result.stats["Total Trades"] >= 1


def test_run_backtest_rejects_empty() -> None:
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    with pytest.raises(ValueError, match="No candles"):
        run_backtest(empty, DonchianStrategy())


def test_run_backtest_respects_init_cash() -> None:
    df = _trending_candles()
    result = run_backtest(df, DonchianStrategy(), init_cash=5000, granularity="H1")
    # vectorbt reports Start Value matching our init cash.
    assert result.stats["Start Value"] == pytest.approx(5000.0)
