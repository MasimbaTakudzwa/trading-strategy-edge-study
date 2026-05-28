"""Broker adapter interface.

Strategies never import a specific broker. They emit Intents, the risk gate
validates them, the OMS turns approved Intents into Orders, and an adapter
implementing this protocol sends them to a broker.

Swapping OANDA → IBKR (for futures) means adding a new adapter — no changes
to strategy, risk, or OMS code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


@dataclass(frozen=True)
class OrderRequest:
    """What the OMS sends to a broker adapter."""

    client_order_id: str  # our idempotency key
    instrument: str
    side: Side
    units: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None


@dataclass(frozen=True)
class OrderResult:
    """What the adapter returns to the OMS."""

    client_order_id: str
    broker_order_id: str | None
    status: str  # filled, pending, rejected
    filled_price: float | None
    filled_at: datetime | None
    rejection_reason: str | None
    raw_response: dict


@dataclass(frozen=True)
class Position:
    instrument: str
    units: float  # positive = long, negative = short
    avg_entry_price: float
    unrealized_pnl: float


@dataclass(frozen=True)
class AccountSnapshot:
    balance: float
    equity: float
    unrealized_pnl: float
    margin_used: float
    open_positions: int


class Broker(Protocol):
    """Interface every broker adapter must implement."""

    def place_order(self, request: OrderRequest) -> OrderResult: ...

    def cancel_order(self, broker_order_id: str) -> bool: ...

    def get_positions(self) -> list[Position]: ...

    def get_account(self) -> AccountSnapshot: ...

    def close_position(self, instrument: str) -> int:
        """Close all open positions for one instrument. Returns count closed."""
        ...

    def close_all_positions(self) -> int:
        """Kill switch. Returns the number of positions closed."""
        ...
