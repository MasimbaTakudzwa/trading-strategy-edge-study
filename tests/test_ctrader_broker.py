"""Tests for the cTrader broker adapter with a mocked protocol — verifies
account/position parsing, volume signing, order construction, and the kill
switch without touching the network."""

from __future__ import annotations

from typing import Any

import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAClosePositionReq,
    ProtoOAExecutionEvent,
    ProtoOANewOrderReq,
    ProtoOAReconcileReq,
    ProtoOAReconcileRes,
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
    ProtoOATraderReq,
    ProtoOATraderRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAPositionStatus,
    ProtoOATradeSide,
)

from trading_bot.execution.base import OrderRequest, OrderType, Side

ACCOUNT = 1234567  # placeholder demo id; the real value lives only in .env
SYMBOLS = {"EURUSD": 1, "XAUUSD": 41}


def _trader_res(balance: int = 99863, money_digits: int = 2) -> ProtoOATraderRes:
    r = ProtoOATraderRes()
    r.ctidTraderAccountId = ACCOUNT
    r.trader.balance = balance
    r.trader.moneyDigits = money_digits
    return r


def _symbols_res() -> ProtoOASymbolsListRes:
    r = ProtoOASymbolsListRes()
    for n, i in SYMBOLS.items():
        s = r.symbol.add()
        s.symbolId = i
        s.symbolName = n
    return r


def _symbol_by_id_res() -> ProtoOASymbolByIdRes:
    r = ProtoOASymbolByIdRes()
    s = r.symbol.add()
    s.symbolId = 1
    s.digits = 5
    s.pipPosition = 4
    s.minVolume = 100_000
    s.stepVolume = 100_000
    s.maxVolume = 1_000_000_000
    s.lotSize = 10_000_000
    return r


def _reconcile_res(positions: list[tuple]) -> ProtoOAReconcileRes:
    r = ProtoOAReconcileRes()
    for pid, sid, vol, side, price in positions:
        p = r.position.add()
        p.positionId = pid
        p.positionStatus = ProtoOAPositionStatus.POSITION_STATUS_OPEN
        p.price = price
        p.tradeData.symbolId = sid
        p.tradeData.volume = vol
        p.tradeData.tradeSide = side
    return r


class FakeProtocol:
    account_id = ACCOUNT

    def __init__(self, positions: list[tuple] | None = None) -> None:
        self._positions = positions or []
        self.sent: list[Any] = []

    def send(self, msg: Any) -> Any:
        self.sent.append(msg)
        if isinstance(msg, ProtoOASymbolsListReq):
            return _symbols_res()
        if isinstance(msg, ProtoOATraderReq):
            return _trader_res()
        if isinstance(msg, ProtoOAReconcileReq):
            return _reconcile_res(self._positions)
        if isinstance(msg, ProtoOASymbolByIdReq):
            return _symbol_by_id_res()
        if isinstance(msg, (ProtoOANewOrderReq, ProtoOAClosePositionReq)):
            ev = ProtoOAExecutionEvent()
            ev.ctidTraderAccountId = ACCOUNT
            ev.order.orderId = 999
            return ev
        raise AssertionError(f"unexpected message {type(msg).__name__}")


def _broker(positions: list[tuple] | None = None):  # type: ignore[no-untyped-def]
    from trading_bot.execution.ctrader_broker import CTraderBroker

    return CTraderBroker(FakeProtocol(positions))


def test_get_account_scales_balance() -> None:
    acct = _broker().get_account()
    assert acct.balance == pytest.approx(998.63)  # 99863 / 10^2
    assert acct.open_positions == 0


def test_get_positions_signs_by_side() -> None:
    # A long 1,000-unit EURUSD (volume 100,000) and a short 2 oz gold (volume 200).
    broker = _broker(
        [
            (101, 1, 100_000, ProtoOATradeSide.BUY, 1.10),
            (102, 41, 200, ProtoOATradeSide.SELL, 2500.0),
        ]
    )
    pos = {p.instrument: p for p in broker.get_positions()}
    assert pos["EURUSD"].units == pytest.approx(1000.0)  # long → positive
    assert pos["XAUUSD"].units == pytest.approx(-2.0)  # short → negative
    assert pos["EURUSD"].avg_entry_price == pytest.approx(1.10)


def test_close_all_positions_closes_each_open() -> None:
    broker = _broker(
        [
            (101, 1, 100_000, ProtoOATradeSide.BUY, 1.10),
            (102, 41, 200, ProtoOATradeSide.SELL, 2500.0),
        ]
    )
    n = broker.close_all_positions()
    assert n == 2
    closes = [m for m in broker._protocol.sent if isinstance(m, ProtoOAClosePositionReq)]
    assert {c.positionId for c in closes} == {101, 102}


def test_place_order_builds_correct_request() -> None:
    broker = _broker()
    req = OrderRequest(
        client_order_id="abc-123",
        instrument="EURUSD",
        side=Side.BUY,
        units=1000,
        order_type=OrderType.MARKET,
        stop_loss_price=1.0850,
    )
    result = broker.place_order(req)

    sent = [m for m in broker._protocol.sent if isinstance(m, ProtoOANewOrderReq)]
    assert len(sent) == 1
    order = sent[0]
    assert order.symbolId == 1
    assert order.tradeSide == ProtoOATradeSide.BUY
    assert order.volume == 100_000  # 1,000 units × 100
    assert order.clientOrderId == "abc-123"
    assert order.stopLoss == pytest.approx(1.0850)
    assert result.client_order_id == "abc-123"
    assert result.broker_order_id == "999"
