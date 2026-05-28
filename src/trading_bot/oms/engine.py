"""Order Management System — the single choke point every order passes through.

Turns a strategy Intent into a live order by, in strict order:
  1. ATR-sizing the position (constant money-at-risk)
  2. fitting to broker constraints + the small-account min-size guard
  3. deriving the stop price from the ATR stop distance
  4. minting a deterministic, idempotent client_order_id
  5. running the risk gate (kill switch, max positions, leverage, stop required)
  6. placing via the broker (which itself runs the live-trading safety guard)

Any failed gate short-circuits and returns a non-placed result with a reason —
the order never reaches the broker. The idempotent id means a retry of the same
intent on the same bar can't double-submit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from trading_bot.execution.base import Broker, OrderRequest, OrderResult, OrderType, Side
from trading_bot.execution.instruments import SymbolSpec, fit_order_size
from trading_bot.observability.logging import get_logger
from trading_bot.risk.limits import Intent, RiskDecision, RiskGate

log = get_logger(__name__)


@dataclass
class OMSResult:
    placed: bool
    reason: str | None = None
    order_request: OrderRequest | None = None
    order_result: OrderResult | None = None


def client_order_id(run_id: str, instrument: str, bar_ts: datetime, side: Side) -> str:
    """Deterministic id from (run, instrument, bar, side) so re-processing the
    same bar produces the same id — the broker rejects the duplicate."""
    raw = f"{run_id}:{instrument}:{bar_ts.isoformat()}:{side.value}"
    return hashlib.sha1(raw.encode()).hexdigest()[:32]


class OMS:
    def __init__(
        self,
        broker: Broker,
        gate: RiskGate,
        run_id: str,
        *,
        value_per_point: float = 1.0,
    ) -> None:
        self._broker = broker
        self._gate = gate
        self._run_id = run_id
        self._value_per_point = value_per_point

    def open_position(
        self,
        intent: Intent,
        *,
        equity: float,
        entry_price: float,
        atr_value: float,
        spec: SymbolSpec,
        bar_ts: datetime,
        open_positions: int = 0,
        daily_pnl_pct: float = 0.0,
        leverage: float = 1.0,
    ) -> OMSResult:
        # 1. ATR sizing → desired units + stop distance.
        desired_units, stop_distance = self._gate.size_from_atr(
            account_equity=equity,
            atr_value=atr_value,
            value_per_point=self._value_per_point,
        )

        # 2. Fit to broker constraints + small-account min-size guard.
        sized = fit_order_size(
            desired_units,
            spec,
            stop_distance=stop_distance,
            value_per_point=self._value_per_point,
            equity=equity,
            max_risk_fraction=self._gate.max_account_risk_pct,
        )
        if not sized.tradeable:
            log.warning("oms_refused", instrument=intent.instrument, reason=sized.reason)
            return OMSResult(placed=False, reason=sized.reason)

        # 3. Stop price from the ATR stop distance (digits-rounded).
        if intent.side == Side.BUY:
            stop_price = entry_price - stop_distance
        else:
            stop_price = entry_price + stop_distance
        stop_price = round(stop_price, spec.digits)

        # 4. Idempotent order id + request.
        request = OrderRequest(
            client_order_id=client_order_id(self._run_id, intent.instrument, bar_ts, intent.side),
            instrument=intent.instrument,
            side=intent.side,
            units=sized.units,
            order_type=OrderType.MARKET,
            stop_loss_price=stop_price,
            take_profit_price=intent.take_profit_price,
        )

        # 5. Risk gate.
        check = self._gate.check(
            request,
            open_positions=open_positions,
            daily_pnl_pct=daily_pnl_pct,
            leverage=leverage,
        )
        if check.decision != RiskDecision.APPROVE:
            log.warning(
                "oms_blocked",
                instrument=intent.instrument,
                decision=check.decision.value,
                reason=check.reason,
            )
            return OMSResult(placed=False, reason=check.reason)

        # 6. Place.
        result = self._broker.place_order(request)
        log.info(
            "oms_placed",
            instrument=intent.instrument,
            side=intent.side.value,
            units=sized.units,
            stop=stop_price,
            client_order_id=request.client_order_id,
        )
        return OMSResult(placed=True, order_request=request, order_result=result)

    def close_position(self, instrument: str) -> int:
        """Exit an open position. Delegates to the broker (risk-reducing)."""
        return self._broker.close_position(instrument)
