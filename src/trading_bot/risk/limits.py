"""Risk gate. Every order — backtest, paper, or live — passes through here
before reaching a broker adapter.

Three jobs:
  1. Size positions according to per-trade risk budget and ATR-based stops.
  2. Reject orders that breach per-trade, per-day, or aggregate limits.
  3. Trigger the kill switch when daily drawdown breaches threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from trading_bot.config import get_settings
from trading_bot.execution.base import OrderRequest, Side
from trading_bot.observability.logging import get_logger

log = get_logger(__name__)


class RiskDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    KILL = "kill"  # whole bot must shut down


@dataclass(frozen=True)
class RiskCheck:
    decision: RiskDecision
    reason: str | None = None


@dataclass(frozen=True)
class Intent:
    """Strategy output — what the strategy *wants* to do.

    The risk gate converts this into a sized OrderRequest, or rejects it.
    Units are computed here, not in the strategy.
    """

    instrument: str
    side: Side
    stop_loss_price: float
    take_profit_price: float | None = None
    metadata: dict | None = None


class RiskGate:
    """Validates Intents and converts them to sized OrderRequests."""

    def __init__(self) -> None:
        s = get_settings()
        self.max_account_risk_pct = s.max_account_risk_pct
        self.max_daily_loss_pct = s.max_daily_loss_pct
        self.max_open_positions = s.max_open_positions
        self.max_leverage = s.max_leverage

    def size_position(
        self,
        intent: Intent,
        account_equity: float,
        entry_price: float,
        pip_value: float,
    ) -> float:
        """Volatility-targeted sizing: risk a fixed % of equity per trade.

        units = (equity × risk_pct) / (stop_distance × pip_value)

        TODO(week-3): full pip-value lookup per instrument, contract-size aware.
        """
        risk_amount = account_equity * self.max_account_risk_pct
        stop_distance = abs(entry_price - intent.stop_loss_price)
        if stop_distance <= 0:
            raise ValueError("Stop-loss must differ from entry price")
        return risk_amount / (stop_distance * pip_value)

    def check(
        self,
        request: OrderRequest,
        open_positions: int,
        daily_pnl_pct: float,
        leverage: float,
    ) -> RiskCheck:
        """Run all gates. Order matters — kill switch fires first."""
        if daily_pnl_pct <= -self.max_daily_loss_pct:
            return RiskCheck(
                RiskDecision.KILL,
                f"Daily loss {daily_pnl_pct:.2%} breached limit {-self.max_daily_loss_pct:.2%}",
            )
        if open_positions >= self.max_open_positions:
            return RiskCheck(
                RiskDecision.REJECT,
                f"Already at max_open_positions={self.max_open_positions}",
            )
        if leverage > self.max_leverage:
            return RiskCheck(
                RiskDecision.REJECT,
                f"Leverage {leverage:.1f}x exceeds cap {self.max_leverage:.1f}x",
            )
        if request.stop_loss_price is None:
            return RiskCheck(RiskDecision.REJECT, "Stop-loss missing — required by policy")
        return RiskCheck(RiskDecision.APPROVE)
