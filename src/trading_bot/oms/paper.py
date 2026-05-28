"""Paper-trading engine — the live decision loop (demo-account edition).

One *tick* is one decision cycle on the latest closed bar:

  1. read the broker's current account + position (broker is the source of truth)
  2. compute the strategy's signals over the trailing candle window
  3. turn the latest bar's signal + current position into one decision
     (open long / open short / close / hold)
  4. act through the OMS (which sizes, risk-checks, and places) or close
  5. persist an account snapshot, and the order if one was placed

`run_tick` takes candles as input and is pure w.r.t. I/O beyond the injected
broker/oms/store — so it tests with a fake broker and a hand-built DataFrame,
no network and no database. `run_loop` is the thin scheduler that fetches fresh
candles and calls `run_tick` on a cadence.

Why reading the *last* bar is safe: both strategies compare bar-t's close to
channels/bands built from bars t-1 and earlier (`.shift(1)`), so the signal on
the most recent closed bar is fully determined and actionable now — no
look-ahead. A single position per instrument is held; an entry signal while
already in a position is ignored (we only act on the matching exit).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

import pandas as pd

from trading_bot.execution.base import AccountSnapshot, Side
from trading_bot.execution.instruments import SymbolSpec
from trading_bot.observability.logging import get_logger
from trading_bot.oms.engine import OMS, OMSResult
from trading_bot.oms.store import RunStore
from trading_bot.risk.limits import Intent
from trading_bot.risk.sizing import atr
from trading_bot.strategies.base import SignalSet, SignalStrategy

log = get_logger(__name__)


class Action(str, Enum):
    """What the engine did on a tick."""

    OPEN = "open"
    CLOSE = "close"
    HOLD = "hold"
    SKIP = "skip"  # not enough data, or sizing inputs not ready


@dataclass
class TickResult:
    action: Action
    bar_ts: datetime | None = None
    side: Side | None = None
    equity: float | None = None
    open_units: float = 0.0
    reason: str | None = None
    oms_result: OMSResult | None = None
    closed: int | None = None


class _Decision(Enum):
    OPEN_LONG = 1
    OPEN_SHORT = 2
    CLOSE = 3
    HOLD = 4


def _decide(signals: SignalSet, open_units: float, tol: float = 1e-9) -> _Decision:
    """Map the latest bar's signals + current position to a single decision.

    Single-position state machine:
      - flat  + long entry  → open long
      - flat  + short entry → open short
      - long  + long exit   → close
      - short + short exit   → close
      - anything else        → hold (entries are ignored while in a position)
    """
    long_entry = bool(signals.long_entries.iloc[-1])
    short_entry = bool(signals.short_entries.iloc[-1])
    long_exit = bool(signals.long_exits.iloc[-1])
    short_exit = bool(signals.short_exits.iloc[-1])

    if abs(open_units) <= tol:  # flat
        if long_entry:
            return _Decision.OPEN_LONG
        if short_entry:
            return _Decision.OPEN_SHORT
        return _Decision.HOLD
    if open_units > tol and long_exit:  # long → exit
        return _Decision.CLOSE
    if open_units < -tol and short_exit:  # short → exit
        return _Decision.CLOSE
    return _Decision.HOLD


def _as_datetime(ts: object) -> datetime:
    """pandas Timestamp → python datetime (Timestamp is a datetime subclass,
    but normalising keeps downstream isoformat()/hashing predictable)."""
    to_py = getattr(ts, "to_pydatetime", None)
    return to_py() if to_py else ts  # type: ignore[return-value]


class PaperEngine:
    """Drives one strategy on one instrument against a (demo) broker."""

    def __init__(
        self,
        *,
        broker,  # type: ignore[no-untyped-def]  # Broker protocol
        oms: OMS,
        strategy: SignalStrategy,
        instrument: str,
        spec: SymbolSpec,
        store: RunStore,
        run_id: str,
        env: str = "practice",
        atr_period: int = 14,
        min_candles: int | None = None,
        leverage: float = 1.0,
    ) -> None:
        self._broker = broker
        self._oms = oms
        self._strategy = strategy
        self._instrument = instrument
        self._spec = spec
        self._store = store
        self._run_id = run_id
        self._env = env
        self._atr_period = atr_period
        # Enough bars for a defined ATR; strategies self-guard via NaN→no-signal.
        self._min_candles = min_candles if min_candles is not None else atr_period + 2
        self._leverage = leverage
        self._day: date | None = None
        self._day_start_equity: float | None = None

    def run_tick(self, candles: pd.DataFrame) -> TickResult:
        if len(candles) < self._min_candles:
            return TickResult(
                Action.SKIP,
                reason=f"need >= {self._min_candles} candles, have {len(candles)}",
            )

        last_ts = _as_datetime(candles.index[-1])
        account: AccountSnapshot = self._broker.get_account()
        equity = account.equity
        open_units = self._open_units()
        daily_pnl_pct = self._update_daily_pnl(equity, last_ts)

        signals = self._strategy.generate_signals(candles)
        decision = _decide(signals, open_units)

        # Heartbeat: record account state on every evaluated tick.
        self._store.record_snapshot(run_id=self._run_id, env=self._env, snapshot=account)

        if decision in (_Decision.OPEN_LONG, _Decision.OPEN_SHORT):
            side = Side.BUY if decision is _Decision.OPEN_LONG else Side.SELL
            return self._open(side, candles, last_ts, equity, open_units, daily_pnl_pct, account)

        if decision is _Decision.CLOSE:
            closed = self._oms.close_position(self._instrument)
            self._store.record_event(
                run_id=self._run_id,
                env=self._env,
                level="info",
                category="position_closed",
                message=f"closed {self._instrument}",
                context={"bar_ts": last_ts.isoformat(), "closed": closed},
            )
            log.info(
                "paper_close",
                instrument=self._instrument,
                closed=closed,
                bar_ts=last_ts.isoformat(),
            )
            return TickResult(
                Action.CLOSE,
                bar_ts=last_ts,
                equity=equity,
                open_units=open_units,
                closed=closed,
            )

        return TickResult(Action.HOLD, bar_ts=last_ts, equity=equity, open_units=open_units)

    def _open(
        self,
        side: Side,
        candles: pd.DataFrame,
        last_ts: datetime,
        equity: float,
        open_units: float,
        daily_pnl_pct: float,
        account: AccountSnapshot,
    ) -> TickResult:
        atr_value = float(atr(candles, self._atr_period).iloc[-1])
        if pd.isna(atr_value) or atr_value <= 0:
            return TickResult(
                Action.SKIP,
                bar_ts=last_ts,
                equity=equity,
                open_units=open_units,
                reason="ATR not ready",
            )

        entry_price = float(candles["close"].iloc[-1])
        intent = Intent(self._instrument, side)
        res = self._oms.open_position(
            intent,
            equity=equity,
            entry_price=entry_price,
            atr_value=atr_value,
            spec=self._spec,
            bar_ts=last_ts,
            open_positions=account.open_positions,
            daily_pnl_pct=daily_pnl_pct,
            leverage=self._leverage,
        )
        if res.placed and res.order_request is not None:
            self._store.record_order(
                run_id=self._run_id,
                env=self._env,
                request=res.order_request,
                result=res.order_result,
            )
        log.info(
            "paper_open",
            instrument=self._instrument,
            side=side.value,
            placed=res.placed,
            reason=res.reason,
            bar_ts=last_ts.isoformat(),
        )
        return TickResult(
            Action.OPEN,
            bar_ts=last_ts,
            side=side,
            equity=equity,
            open_units=open_units,
            oms_result=res,
            reason=res.reason,
        )

    def _open_units(self) -> float:
        """Signed units currently held in this engine's instrument (0 if flat)."""
        return sum(
            p.units for p in self._broker.get_positions() if p.instrument == self._instrument
        )

    def _update_daily_pnl(self, equity: float, ts: datetime) -> float:
        """Track the UTC day's opening equity; return drawdown vs it for the
        kill switch. Resets on day rollover. In-memory only — a restart resets
        the baseline (acceptable for paper; DB-backed baseline is a later step)."""
        day = ts.date()
        if self._day != day or self._day_start_equity is None:
            self._day = day
            self._day_start_equity = equity
            return 0.0
        if self._day_start_equity <= 0:
            return 0.0
        return (equity - self._day_start_equity) / self._day_start_equity

    def run_loop(
        self,
        load_candles: Callable[[], pd.DataFrame],
        *,
        poll_seconds: float,
        max_ticks: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_tick: Callable[[TickResult], None] | None = None,
    ) -> int:
        """Poll on a cadence: load fresh candles, run a tick, repeat.

        Polling faster than the bar interval is harmless — once a position is
        open the broker reports it and the next tick holds; and a re-issued
        order carries the same idempotent client_order_id. Returns tick count.
        Ctrl-C exits cleanly.
        """
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                result = self.run_tick(load_candles())
                if on_tick is not None:
                    on_tick(result)
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                sleep(poll_seconds)
        except KeyboardInterrupt:
            log.info("paper_loop_interrupted", instrument=self._instrument, ticks=ticks)
        return ticks
