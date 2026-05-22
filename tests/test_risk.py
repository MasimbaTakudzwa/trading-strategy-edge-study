"""Risk gate tests. These check the limits enforced *before* any order
reaches a broker — they protect against strategies blowing up the account."""

from __future__ import annotations

import pytest

from trading_bot.execution.base import OrderRequest, OrderType, Side
from trading_bot.risk.limits import Intent, RiskDecision, RiskGate


def make_request(stop_loss: float | None = 1.0900) -> OrderRequest:
    return OrderRequest(
        client_order_id="test-1",
        instrument="EUR_USD",
        side=Side.BUY,
        units=10_000,
        order_type=OrderType.MARKET,
        stop_loss_price=stop_loss,
    )


def test_kill_switch_on_daily_loss_breach() -> None:
    gate = RiskGate()
    result = gate.check(
        make_request(),
        open_positions=0,
        daily_pnl_pct=-0.05,  # 5% loss
        leverage=1.0,
    )
    assert result.decision == RiskDecision.KILL


def test_reject_when_max_positions_reached() -> None:
    gate = RiskGate()
    result = gate.check(
        make_request(),
        open_positions=gate.max_open_positions,
        daily_pnl_pct=0.0,
        leverage=1.0,
    )
    assert result.decision == RiskDecision.REJECT


def test_reject_when_no_stop_loss() -> None:
    gate = RiskGate()
    result = gate.check(
        make_request(stop_loss=None),
        open_positions=0,
        daily_pnl_pct=0.0,
        leverage=1.0,
    )
    assert result.decision == RiskDecision.REJECT
    assert result.reason is not None
    assert "stop-loss" in result.reason.lower()


def test_approve_within_limits() -> None:
    gate = RiskGate()
    result = gate.check(
        make_request(),
        open_positions=0,
        daily_pnl_pct=0.0,
        leverage=1.0,
    )
    assert result.decision == RiskDecision.APPROVE


def test_size_position_uses_risk_budget() -> None:
    gate = RiskGate()
    intent = Intent(instrument="EUR_USD", side=Side.BUY, stop_loss_price=1.0850)
    units = gate.size_position(
        intent,
        account_equity=10_000,
        entry_price=1.0900,
        pip_value=1.0,
    )
    # 10000 * 0.005 / (0.005 * 1.0) = 10000 — sane order of magnitude
    assert units > 0
    assert units == pytest.approx(10_000.0, rel=1e-6)


def test_size_position_rejects_zero_stop_distance() -> None:
    gate = RiskGate()
    intent = Intent(instrument="EUR_USD", side=Side.BUY, stop_loss_price=1.0900)
    with pytest.raises(ValueError):
        gate.size_position(intent, account_equity=10_000, entry_price=1.0900, pip_value=1.0)
