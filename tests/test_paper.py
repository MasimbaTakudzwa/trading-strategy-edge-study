"""Tests for the paper-trading engine.

Fake broker + fake store + a strategy whose signals we control, so each tick's
decision is deterministic. Covers the decision state machine, the open/close/
hold/skip outcomes, persistence side-effects, the kill-switch wiring, and the
loop scheduler — all without a network or a database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from trading_bot.execution.base import AccountSnapshot, OrderResult, Position, Side
from trading_bot.execution.instruments import SymbolSpec
from trading_bot.oms.engine import OMS
from trading_bot.oms.paper import Action, PaperEngine, _decide, _Decision
from trading_bot.risk.limits import RiskGate
from trading_bot.strategies.base import SignalSet

TS = datetime(2024, 1, 1, tzinfo=timezone.utc)
# Same literal as test_oms. min_volume=100_000 → 1000-unit minimum & step.
EURUSD = SymbolSpec(1, "EURUSD", 5, 4, 100_000, 100_000, 1_000_000_000, 10_000_000)


# -- fakes -------------------------------------------------------------------


class FakeBroker:
    def __init__(
        self,
        *,
        equity: float = 1000.0,
        balance: float = 1000.0,
        positions: list[Position] | None = None,
    ) -> None:
        self.equity = equity
        self.balance = balance
        self._positions = positions or []
        self.placed: list[Any] = []
        self.closed: list[str] = []

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            balance=self.balance,
            equity=self.equity,
            unrealized_pnl=0.0,
            margin_used=0.0,
            open_positions=len(self._positions),
        )

    def get_positions(self) -> list[Position]:
        return list(self._positions)

    def place_order(self, req: Any) -> OrderResult:
        self.placed.append(req)
        return OrderResult(req.client_order_id, "1", "filled", 1.10, None, None, {})

    def close_position(self, instrument: str) -> int:
        self.closed.append(instrument)
        return 1


class FakeStore:
    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.orders: list[dict] = []
        self.snapshots: list[dict] = []
        self.events: list[dict] = []
        self.ended: list[str] = []

    def start_run(self, **kw: Any) -> str:
        self.runs.append(kw)
        return "run-123"

    def end_run(self, run_id: str) -> None:
        self.ended.append(run_id)

    def record_order(self, **kw: Any) -> None:
        self.orders.append(kw)

    def record_snapshot(self, **kw: Any) -> None:
        self.snapshots.append(kw)

    def record_event(self, **kw: Any) -> None:
        self.events.append(kw)


class FakeStrategy:
    """Returns a pre-built signal set regardless of candles."""

    name = "fake"

    def __init__(self, signals: SignalSet) -> None:
        self._signals = signals

    def generate_signals(self, candles: pd.DataFrame) -> SignalSet:
        return self._signals


# -- builders ----------------------------------------------------------------


def _candles(n: int = 30, close: float = 1.10, half_range: float = 0.001) -> pd.DataFrame:
    """Flat-close OHLCV. True range is `2*half_range` every bar, so ATR = 0.002
    by default → a 0.004 stop, min-risk ~$4 < $5 budget (comfortably tradeable,
    off the float knife-edge where min-risk == budget)."""
    idx = pd.date_range(start=TS, periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": [close] * n,
            "high": [close + half_range] * n,
            "low": [close - half_range] * n,
            "close": [close] * n,
            "volume": [1000] * n,
        },
        index=idx,
    )


def _sig(
    index: pd.Index,
    *,
    long_entry: bool = False,
    short_entry: bool = False,
    long_exit: bool = False,
    short_exit: bool = False,
) -> SignalSet:
    """Signal set that is all-False except the chosen flag(s) on the LAST bar."""

    def s(flag: bool) -> pd.Series:
        arr = [False] * len(index)
        if len(index):
            arr[-1] = flag
        return pd.Series(arr, index=index, dtype=bool)

    return SignalSet(
        long_entries=s(long_entry),
        long_exits=s(long_exit),
        short_entries=s(short_entry),
        short_exits=s(short_exit),
    )


def _engine(broker: FakeBroker, strategy: FakeStrategy, store: FakeStore) -> PaperEngine:
    oms = OMS(broker, RiskGate(), run_id="practice:fake:EURUSD", value_per_point=1.0)
    return PaperEngine(
        broker=broker,
        oms=oms,
        strategy=strategy,
        instrument="EURUSD",
        spec=EURUSD,
        store=store,
        run_id="run-123",
        env="practice",
    )


def _long(units: float = 1000.0) -> Position:
    return Position(instrument="EURUSD", units=units, avg_entry_price=1.10, unrealized_pnl=0.0)


def _short(units: float = -1000.0) -> Position:
    return Position(instrument="EURUSD", units=units, avg_entry_price=1.10, unrealized_pnl=0.0)


# -- decision state machine --------------------------------------------------


def test_decide_covers_the_single_position_state_machine() -> None:
    idx = pd.date_range(TS, periods=3, freq="h", tz="UTC")
    assert _decide(_sig(idx, long_entry=True), 0.0) is _Decision.OPEN_LONG
    assert _decide(_sig(idx, short_entry=True), 0.0) is _Decision.OPEN_SHORT
    assert _decide(_sig(idx), 0.0) is _Decision.HOLD
    assert _decide(_sig(idx, long_exit=True), 1000.0) is _Decision.CLOSE
    assert _decide(_sig(idx, short_exit=True), -1000.0) is _Decision.CLOSE
    # Entry signals are ignored while already in a position.
    assert _decide(_sig(idx, short_entry=True), 1000.0) is _Decision.HOLD
    assert _decide(_sig(idx, long_entry=True), -1000.0) is _Decision.HOLD
    # Exit signal for the wrong side does nothing.
    assert _decide(_sig(idx, long_exit=True), 0.0) is _Decision.HOLD


# -- run_tick outcomes -------------------------------------------------------


def test_flat_plus_entry_signal_opens_sizes_and_persists() -> None:
    broker = FakeBroker(equity=1000.0)
    store = FakeStore()
    candles = _candles()
    engine = _engine(broker, FakeStrategy(_sig(candles.index, long_entry=True)), store)

    result = engine.run_tick(candles)

    assert result.action is Action.OPEN
    assert result.side is Side.BUY
    assert result.oms_result is not None and result.oms_result.placed
    # The engine reached the broker and persisted; exact sizing math is test_oms's job.
    assert len(broker.placed) == 1
    order = broker.placed[0]
    assert order.side is Side.BUY
    assert order.units >= EURUSD.min_units  # at least the broker minimum
    assert order.stop_loss_price < 1.10  # long stop sits below entry
    assert len(store.orders) == 1
    assert len(store.snapshots) == 1  # heartbeat recorded


def test_in_long_plus_exit_signal_closes() -> None:
    broker = FakeBroker(positions=[_long()])
    store = FakeStore()
    candles = _candles()
    engine = _engine(broker, FakeStrategy(_sig(candles.index, long_exit=True)), store)

    result = engine.run_tick(candles)

    assert result.action is Action.CLOSE
    assert result.closed == 1
    assert broker.closed == ["EURUSD"]
    assert broker.placed == []  # no new order
    assert any(e["category"] == "position_closed" for e in store.events)


def test_flat_no_signal_holds_but_still_snapshots() -> None:
    broker = FakeBroker()
    store = FakeStore()
    candles = _candles()
    engine = _engine(broker, FakeStrategy(_sig(candles.index)), store)

    result = engine.run_tick(candles)

    assert result.action is Action.HOLD
    assert broker.placed == []
    assert broker.closed == []
    assert len(store.snapshots) == 1


def test_too_few_candles_skips_without_touching_broker() -> None:
    broker = FakeBroker()
    store = FakeStore()
    candles = _candles(n=5)  # below the min for a defined ATR
    engine = _engine(broker, FakeStrategy(_sig(candles.index, long_entry=True)), store)

    result = engine.run_tick(candles)

    assert result.action is Action.SKIP
    assert broker.placed == []
    assert store.snapshots == []  # bailed before any broker/store call


def test_entry_blocked_by_kill_switch_is_not_placed_or_recorded() -> None:
    # Equity 1000 sizes fine, but a -50% daily drawdown trips the kill switch.
    broker = FakeBroker(equity=1000.0)
    store = FakeStore()
    candles = _candles()
    engine = _engine(broker, FakeStrategy(_sig(candles.index, long_entry=True)), store)
    # Seed the day's opening equity high so this tick reads as a deep drawdown.
    engine._day = candles.index[-1].to_pydatetime().date()
    engine._day_start_equity = 2000.0

    result = engine.run_tick(candles)

    assert result.action is Action.OPEN  # a decision was made...
    assert result.oms_result is not None and not result.oms_result.placed  # ...but blocked
    assert "daily loss" in (result.reason or "").lower()
    assert broker.placed == []
    assert store.orders == []  # only placed orders are recorded
    assert len(store.snapshots) == 1  # heartbeat still recorded


# -- loop scheduler ----------------------------------------------------------


def test_run_loop_runs_max_ticks_without_sleeping() -> None:
    broker = FakeBroker()
    store = FakeStore()
    candles = _candles()
    engine = _engine(broker, FakeStrategy(_sig(candles.index)), store)

    seen: list[Action] = []
    ticks = engine.run_loop(
        lambda: candles,
        poll_seconds=0.0,
        max_ticks=3,
        sleep=lambda _s: None,
        on_tick=lambda r: seen.append(r.action),
    )

    assert ticks == 3
    assert seen == [Action.HOLD, Action.HOLD, Action.HOLD]
