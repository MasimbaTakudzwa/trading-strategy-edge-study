"""Instrument specs and the bridge between risk-sized units and broker volume.

Two cTrader facts this module encapsulates (both verified against live specs):

  - **Volume is units × 100.** cTrader order volume is in 1/100 of a base
    unit. 1,000 units of EURUSD (a 0.01 lot) is volume 100,000; 1 oz of gold
    is volume 100. Orders must be a multiple of `step_volume` and within
    [min_volume, max_volume].

  - **Minimum sizes can blow a small account's risk budget.** The smallest
    tradeable gold position (1 oz) risks ~$150 on a normal stop — 15% of a
    $1,000 account. fit_order_size() REFUSES any order whose minimum size
    would exceed the per-trade risk budget. This is the guard that stops the
    bot taking a position it can't size safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_bot.observability.logging import get_logger
from trading_bot.risk.sizing import volatility_position_size

log = get_logger(__name__)

# cTrader API volume is in 1/100 of a base unit, universally.
VOLUME_PER_UNIT = 100


@dataclass(frozen=True)
class SymbolSpec:
    symbol_id: int
    name: str
    digits: int
    pip_position: int
    min_volume: int  # in API volume (centi-units)
    step_volume: int
    max_volume: int
    lot_size: int

    @property
    def min_units(self) -> float:
        return self.min_volume / VOLUME_PER_UNIT

    @property
    def step_units(self) -> float:
        return self.step_volume / VOLUME_PER_UNIT


def units_to_api_volume(units: float, spec: SymbolSpec) -> int:
    """Convert base units to a broker-legal API volume: ×100, floored to a
    step multiple, clamped to [min, max]. Returns 0 if below one step."""
    raw = round(units * VOLUME_PER_UNIT)
    stepped = (raw // spec.step_volume) * spec.step_volume
    return int(min(max(stepped, 0), spec.max_volume))


def api_volume_to_units(api_volume: int) -> float:
    return api_volume / VOLUME_PER_UNIT


@dataclass(frozen=True)
class SizedOrder:
    units: float  # base units to trade (0 if untradeable)
    api_volume: int  # cTrader volume (0 if untradeable)
    risk_amount: float  # money at risk at this size
    tradeable: bool
    reason: str | None = None


def fit_order_size(
    desired_units: float,
    spec: SymbolSpec,
    *,
    stop_distance: float,
    value_per_point: float,
    equity: float,
    max_risk_fraction: float,
) -> SizedOrder:
    """Reconcile the risk-sized position with broker constraints.

    Refuses the trade if the *smallest* tradeable size already risks more than
    the budget (the small-account guard). Otherwise rounds the desired size
    down to a legal step (never below the minimum).
    """
    budget = equity * max_risk_fraction
    min_risk = spec.min_units * stop_distance * value_per_point

    if min_risk > budget:
        return SizedOrder(
            units=0.0,
            api_volume=0,
            risk_amount=min_risk,
            tradeable=False,
            reason=(
                f"Min size {spec.min_units:g} units risks {min_risk:.2f} > "
                f"budget {budget:.2f} ({max_risk_fraction:.2%} of {equity:.2f}). "
                f"Account too small for this instrument."
            ),
        )

    units = max(desired_units, spec.min_units)
    api_volume = units_to_api_volume(units, spec)
    if api_volume < spec.min_volume:
        api_volume = spec.min_volume
    units = api_volume_to_units(api_volume)
    risk = units * stop_distance * value_per_point
    return SizedOrder(units=units, api_volume=api_volume, risk_amount=risk, tradeable=True)


def fetch_symbol_spec(protocol: Any, symbol_id: int, name: str = "") -> SymbolSpec:
    """Fetch a symbol's trading specs from cTrader (digits, volume bounds)."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolByIdReq

    req = ProtoOASymbolByIdReq()
    req.ctidTraderAccountId = protocol.account_id
    req.symbolId.append(symbol_id)
    res = protocol.send(req)
    s = res.symbol[0]
    return SymbolSpec(
        symbol_id=symbol_id,
        name=name,
        digits=s.digits,
        pip_position=s.pipPosition,
        min_volume=s.minVolume,
        step_volume=s.stepVolume,
        max_volume=s.maxVolume,
        lot_size=s.lotSize,
    )
