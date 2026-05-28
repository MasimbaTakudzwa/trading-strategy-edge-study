"""Vectorbt-backed backtest runner.

Takes a strategy's signals plus an OHLCV frame and simulates a long/short
portfolio with realistic costs, then returns vectorbt's stats plus the
Portfolio object for further analysis or plotting.

Cost model defaults approximate an FP Markets cTrader **Raw** account:
  - commission ≈ $3 per lot per side. 1 lot = 100k notional, so $3/100k =
    0.00003 as a fraction → `fees=0.00003` per side.
  - raw spread ≈ 0.1 pip on EURUSD ≈ 0.00001/1.10 ≈ 0.00001 of price per
    side → modelled as `slippage`, rounded up to 0.00002 for a buffer.
These are deliberately a touch conservative; tune per instrument later.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import vectorbt as vbt

from trading_bot.observability.logging import get_logger
from trading_bot.strategies.base import SignalStrategy

log = get_logger(__name__)

# Default cTrader Raw cost model (per side, as fractions of trade value).
DEFAULT_FEES = 0.00003
DEFAULT_SLIPPAGE = 0.00002

# Our granularity strings → pandas offset aliases for vectorbt annualisation.
GRANULARITY_TO_FREQ: dict[str, str] = {
    "M1": "1min",
    "M2": "2min",
    "M3": "3min",
    "M4": "4min",
    "M5": "5min",
    "M10": "10min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "H12": "12h",
    "D1": "1d",
    "W1": "1W",
}


@dataclass
class BacktestResult:
    stats: pd.Series  # vectorbt's summary metrics
    portfolio: vbt.Portfolio  # full object for plotting / drilldown


def freq_for(granularity: str) -> str:
    try:
        return GRANULARITY_TO_FREQ[granularity]
    except KeyError as e:
        raise ValueError(f"No freq mapping for granularity {granularity!r}") from e


def run_backtest(
    candles: pd.DataFrame,
    strategy: SignalStrategy,
    *,
    init_cash: float = 1000.0,
    fees: float = DEFAULT_FEES,
    slippage: float = DEFAULT_SLIPPAGE,
    granularity: str = "H1",
    stop_loss: float | None = None,
) -> BacktestResult:
    """Run `strategy` over `candles` and return stats + the portfolio.

    `candles` is a time-indexed OHLCV frame (see data.candles.load_candles).
    `stop_loss` is a fractional hard stop (e.g. 0.02 = 2%); strongly advised
    for mean-reversion, whose failure mode is a non-reverting trend.
    """
    if candles.empty:
        raise ValueError("No candles to backtest — fetch data first.")

    signals = strategy.generate_signals(candles)

    pf = vbt.Portfolio.from_signals(
        close=candles["close"],
        entries=signals.long_entries,
        exits=signals.long_exits,
        short_entries=signals.short_entries,
        short_exits=signals.short_exits,
        init_cash=init_cash,
        fees=fees,
        slippage=slippage,
        sl_stop=stop_loss,
        freq=freq_for(granularity),
    )

    log.info(
        "backtest_complete",
        strategy=strategy.name,
        bars=len(candles),
        init_cash=init_cash,
        fees=fees,
        slippage=slippage,
        stop_loss=stop_loss,
    )
    return BacktestResult(stats=pf.stats(), portfolio=pf)
