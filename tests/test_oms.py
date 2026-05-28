"""Tests for the OMS — the order choke point. Mock broker, real risk gate.
Verifies sizing, the min-size refusal, kill-switch blocking, idempotent ids,
and stop derivation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trading_bot.execution.base import OrderRequest, OrderResult, Side
from trading_bot.execution.instruments import SymbolSpec
from trading_bot.oms.engine import OMS, client_order_id
from trading_bot.risk.limits import Intent, RiskGate

TS = datetime(2024, 1, 1, tzinfo=timezone.utc)
EURUSD = SymbolSpec(1, "EURUSD", 5, 4, 100_000, 100_000, 1_000_000_000, 10_000_000)
GOLD = SymbolSpec(41, "XAUUSD", 2, 2, 100, 100, 1_000_000, 10_000)


class FakeBroker:
    def __init__(self) -> None:
        self.placed: list[OrderRequest] = []
        self.closed: list[str] = []

    def place_order(self, req: OrderRequest) -> OrderResult:
        self.placed.append(req)
        return OrderResult(req.client_order_id, "1", "filled", 1.0, None, None, {})

    def close_position(self, instrument: str) -> int:
        self.closed.append(instrument)
        return 1

    def __getattr__(self, _name: str) -> Any:  # unused protocol methods
        raise AssertionError("unexpected broker call")


def _oms(broker: FakeBroker) -> OMS:
    return OMS(broker, RiskGate(), run_id="r1", value_per_point=1.0)


def test_open_position_sizes_and_places_when_it_fits() -> None:
    broker = FakeBroker()
    # 0.0025 ATR × 2 = 0.005 (50-pip) stop; 1,000-unit micro lot risks exactly $5 = 0.5%.
    res = _oms(broker).open_position(
        Intent("EURUSD", Side.BUY),
        equity=1000,
        entry_price=1.10,
        atr_value=0.0025,
        spec=EURUSD,
        bar_ts=TS,
    )
    assert res.placed
    assert res.order_request is not None
    assert res.order_request.units == 1000
    assert res.order_request.stop_loss_price == 1.095  # 1.10 - 0.005, long stop below
    assert len(broker.placed) == 1


def test_min_size_guard_refuses_gold_on_small_account() -> None:
    broker = FakeBroker()
    res = _oms(broker).open_position(
        Intent("XAUUSD", Side.BUY),
        equity=1000,
        entry_price=2500,
        atr_value=75,  # ×2 = $150 stop; 1 oz min risks $150 ≫ $5 budget
        spec=GOLD,
        bar_ts=TS,
    )
    assert not res.placed
    assert "too small" in (res.reason or "").lower()
    assert broker.placed == []  # never reached the broker


def test_kill_switch_blocks_order() -> None:
    broker = FakeBroker()
    res = _oms(broker).open_position(
        Intent("EURUSD", Side.BUY),
        equity=1000,
        entry_price=1.10,
        atr_value=0.0025,
        spec=EURUSD,
        bar_ts=TS,
        daily_pnl_pct=-0.05,  # past the 2% daily loss limit
    )
    assert not res.placed
    assert broker.placed == []


def test_short_stop_is_above_entry() -> None:
    broker = FakeBroker()
    res = _oms(broker).open_position(
        Intent("EURUSD", Side.SELL),
        equity=1000,
        entry_price=1.10,
        atr_value=0.0025,
        spec=EURUSD,
        bar_ts=TS,
    )
    assert res.placed
    assert res.order_request.stop_loss_price == 1.105  # short → stop above entry


def test_client_order_id_is_deterministic_and_side_specific() -> None:
    a = client_order_id("r1", "EURUSD", TS, Side.BUY)
    b = client_order_id("r1", "EURUSD", TS, Side.BUY)
    c = client_order_id("r1", "EURUSD", TS, Side.SELL)
    assert a == b  # same inputs → same id (idempotent)
    assert a != c  # different side → different id


def test_close_position_delegates_to_broker() -> None:
    broker = FakeBroker()
    assert _oms(broker).close_position("EURUSD") == 1
    assert broker.closed == ["EURUSD"]
