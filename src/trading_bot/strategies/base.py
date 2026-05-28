"""Strategy interface.

All strategies — backtested, papered, or live — implement the same Protocol.
A strategy is pure: given market state, it emits Intents. It does not size
positions, place orders, or know which environment it's in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from trading_bot.risk.limits import Intent


@dataclass(frozen=True)
class MarketContext:
    """What a strategy sees on every bar."""

    instrument: str
    candles: pd.DataFrame  # ts, open, high, low, close, volume — time-indexed
    open_position_units: float  # signed: positive long, negative short, zero flat


@dataclass(frozen=True)
class SignalSet:
    """Vectorised boolean signal Series aligned to the candle index, for the
    backtest harness (vectorbt). Shared by every signal-based strategy."""

    long_entries: pd.Series
    long_exits: pd.Series
    short_entries: pd.Series
    short_exits: pd.Series


class SignalStrategy(Protocol):
    """A strategy expressed as vectorised signals — the backtest-facing form.

    Any object with a name and a generate_signals(candles) -> SignalSet can be
    fed to backtest.runner.run_backtest. Donchian and mean-reversion both
    satisfy this structurally.
    """

    name: str

    def generate_signals(self, candles: pd.DataFrame) -> SignalSet: ...


class Strategy(Protocol):
    """The event-driven, live-facing form (built when paper trading lands)."""

    name: str

    def on_bar(self, ctx: MarketContext) -> list[Intent]:
        """Called once per closed bar. Returns zero or more Intents.

        Pure function w.r.t. ctx — no broker calls, no DB writes, no I/O.
        That's what makes it backtestable and live-runnable from the same code.
        """
        ...
