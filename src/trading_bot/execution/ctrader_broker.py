"""cTrader Open API broker adapter. Implements the Broker protocol.

Demo/live is decided by config (the protocol's host). Strategy and risk code
never import this directly — they go through the Broker protocol.

Read methods (get_account, get_positions) are implemented and safe to call.
Order placement + kill switch are implemented behind the safety guard; on a
demo account they move fake money, on live they're gated by assert_can_trade.
"""

from __future__ import annotations

from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAClosePositionReq,
    ProtoOANewOrderReq,
    ProtoOAReconcileReq,
    ProtoOASymbolsListReq,
    ProtoOATraderReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOAPositionStatus,
    ProtoOATradeSide,
)

from trading_bot.config import get_settings
from trading_bot.data.ctrader_protocol import CTraderProtocol
from trading_bot.execution.base import (
    AccountSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    Side,
)
from trading_bot.execution.instruments import api_volume_to_units, units_to_api_volume
from trading_bot.observability.logging import get_logger

log = get_logger(__name__)


class CTraderBroker:
    """cTrader Open API implementation of the Broker protocol."""

    def __init__(self, protocol: CTraderProtocol | None = None) -> None:
        self._protocol = protocol or CTraderProtocol.from_settings()
        settings = get_settings()
        self._env = settings.ctrader_env.value
        self._account_id = settings.ctrader_account_id
        self._account_is_live: bool | None = None
        self._id_to_name: dict[int, str] | None = None
        self._name_to_id: dict[str, int] | None = None
        log.info("ctrader_broker_initialised", env=self._env, account_id=self._account_id)

    # -- symbol name/id mapping -------------------------------------------------

    def _ensure_symbol_maps(self) -> None:
        if self._id_to_name is not None:
            return
        res = self._protocol.send(ProtoOASymbolsListReq(ctidTraderAccountId=self._account_id))
        self._id_to_name = {s.symbolId: s.symbolName for s in res.symbol}
        self._name_to_id = {s.symbolName: s.symbolId for s in res.symbol}

    def _symbol_name(self, symbol_id: int) -> str:
        self._ensure_symbol_maps()
        assert self._id_to_name is not None
        return self._id_to_name.get(symbol_id, str(symbol_id))

    def _symbol_id(self, name: str) -> int:
        self._ensure_symbol_maps()
        assert self._name_to_id is not None
        try:
            return self._name_to_id[name]
        except KeyError as e:
            raise ValueError(f"Unknown instrument {name!r}") from e

    # -- safety -----------------------------------------------------------------

    def _guard(self) -> None:
        """Run all safety gates before any order. Raises LiveTradingBlocked
        if unsafe. Never bypass."""
        from trading_bot.risk.safety import assert_can_trade

        assert_can_trade(get_settings(), account_is_live=self._account_is_live)

    # -- read --------------------------------------------------------------------

    def get_account(self) -> AccountSnapshot:
        res = self._protocol.send(ProtoOATraderReq(ctidTraderAccountId=self._account_id))
        t = res.trader
        balance = t.balance / (10**t.moneyDigits)
        positions = self.get_positions()
        # Equity = balance + unrealized PnL; unrealized needs live spot prices,
        # so equity ≈ balance here (exact when flat). Refined when spot
        # streaming lands.
        return AccountSnapshot(
            balance=balance,
            equity=balance,
            unrealized_pnl=0.0,
            margin_used=0.0,
            open_positions=len(positions),
        )

    def get_positions(self) -> list[Position]:
        res = self._protocol.send(ProtoOAReconcileReq(ctidTraderAccountId=self._account_id))
        out: list[Position] = []
        for p in res.position:
            if p.positionStatus != ProtoOAPositionStatus.POSITION_STATUS_OPEN:
                continue
            td = p.tradeData
            units = api_volume_to_units(td.volume)
            signed = units if td.tradeSide == ProtoOATradeSide.BUY else -units
            out.append(
                Position(
                    instrument=self._symbol_name(td.symbolId),
                    units=signed,
                    avg_entry_price=float(p.price),
                    unrealized_pnl=0.0,
                )
            )
        return out

    # -- write -------------------------------------------------------------------

    def place_order(self, request: OrderRequest) -> OrderResult:
        self._guard()
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = self._symbol_id(request.instrument)
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = (
            ProtoOATradeSide.BUY if request.side == Side.BUY else ProtoOATradeSide.SELL
        )
        req.volume = units_to_api_volume(request.units, self._spec(request.instrument))
        req.clientOrderId = request.client_order_id  # idempotency key
        if request.stop_loss_price is not None:
            req.stopLoss = request.stop_loss_price
        if request.take_profit_price is not None:
            req.takeProfit = request.take_profit_price

        log.info(
            "placing_order",
            instrument=request.instrument,
            side=request.side.value,
            units=request.units,
            volume=req.volume,
            client_order_id=request.client_order_id,
        )
        event = self._protocol.send(req)
        return self._parse_execution(event, request)

    def symbol_spec(self, instrument: str):  # type: ignore[no-untyped-def]
        """Public accessor for an instrument's broker spec (units/min/step/digits).

        The OMS and paper engine need this to size and round orders.
        """
        return self._spec(instrument)

    def _spec(self, instrument: str):  # type: ignore[no-untyped-def]
        from trading_bot.execution.instruments import fetch_symbol_spec

        return fetch_symbol_spec(self._protocol, self._symbol_id(instrument), instrument)

    def _parse_execution(self, event, request: OrderRequest) -> OrderResult:  # type: ignore[no-untyped-def]
        from datetime import datetime, timezone

        from google.protobuf.json_format import MessageToDict

        order = getattr(event, "order", None)
        broker_order_id = str(order.orderId) if order and order.orderId else None
        filled_price = None
        if getattr(event, "deal", None) and event.deal.executionPrice:
            filled_price = float(event.deal.executionPrice)
        return OrderResult(
            client_order_id=request.client_order_id,
            broker_order_id=broker_order_id,
            status="filled" if filled_price else "submitted",
            filled_price=filled_price,
            filled_at=datetime.now(tz=timezone.utc) if filled_price else None,
            rejection_reason=None,
            raw_response=MessageToDict(event),
        )

    def cancel_order(self, broker_order_id: str) -> bool:
        raise NotImplementedError("Order cancellation wired in alongside limit orders (later).")

    def close_position(self, instrument: str) -> int:
        """Close all open positions for one instrument. Risk-reducing, so not
        gated. Returns the number closed."""
        sid = self._symbol_id(instrument)
        res = self._protocol.send(ProtoOAReconcileReq(ctidTraderAccountId=self._account_id))
        closed = 0
        for p in res.position:
            if (
                p.positionStatus == ProtoOAPositionStatus.POSITION_STATUS_OPEN
                and p.tradeData.symbolId == sid
            ):
                close = ProtoOAClosePositionReq()
                close.ctidTraderAccountId = self._account_id
                close.positionId = p.positionId
                close.volume = p.tradeData.volume
                self._protocol.send(close)
                closed += 1
        log.info("close_position", instrument=instrument, closed=closed)
        return closed

    def close_all_positions(self) -> int:
        """Kill switch — flatten everything. Risk-reducing, so intentionally
        NOT gated by _guard(): we must always be able to flatten."""
        closed = 0
        res = self._protocol.send(ProtoOAReconcileReq(ctidTraderAccountId=self._account_id))
        for p in res.position:
            if p.positionStatus != ProtoOAPositionStatus.POSITION_STATUS_OPEN:
                continue
            close = ProtoOAClosePositionReq()
            close.ctidTraderAccountId = self._account_id
            close.positionId = p.positionId
            close.volume = p.tradeData.volume
            self._protocol.send(close)
            closed += 1
        log.info("close_all_positions", closed=closed)
        return closed
