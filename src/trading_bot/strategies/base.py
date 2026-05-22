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


class Strategy(Protocol):
    name: str

    def on_bar(self, ctx: MarketContext) -> list[Intent]:
        """Called once per closed bar. Returns zero or more Intents.

        Pure function w.r.t. ctx — no broker calls, no DB writes, no I/O.
        That's what makes it backtestable and live-runnable from the same code.
        """
        ...
