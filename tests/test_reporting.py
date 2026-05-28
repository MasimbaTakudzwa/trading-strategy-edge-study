"""Tests for the pure reporting aggregators — summarize_trades, net_positions.

The DB query wrappers need Postgres, so they aren't unit-tested here; the maths
they delegate to is exercised directly with hand-built trade records.
"""

from __future__ import annotations

from trading_bot.oms.reporting import TradeRecord, net_positions, summarize_trades


def _t(
    *,
    instrument: str = "EURUSD",
    side: str = "buy",
    units: float = 1000.0,
    entry: float = 1.10,
    exit_: float | None = None,
    pnl: float | None = None,
    closed: bool = False,
) -> TradeRecord:
    return TradeRecord(
        instrument=instrument,
        side=side,
        units=units,
        entry_price=entry,
        exit_price=exit_,
        realized_pnl=pnl,
        closed=closed,
    )


def test_net_positions_sums_open_signed_units_and_ignores_closed() -> None:
    rows = [
        _t(side="buy", units=1000, closed=False),  # +1000
        _t(side="buy", units=500, closed=False),  # +500 → 1500
        _t(instrument="XAUUSD", side="sell", units=2, closed=False),  # -2
        _t(side="buy", units=9999, closed=True),  # closed → ignored
    ]
    assert net_positions(rows) == {"EURUSD": 1500.0, "XAUUSD": -2.0}


def test_net_positions_omits_netted_flat() -> None:
    rows = [
        _t(side="buy", units=1000, closed=False),
        _t(side="sell", units=1000, closed=False),
    ]
    assert net_positions(rows) == {}  # +1000 - 1000 = flat → omitted


def test_summarize_trades_computes_headline_metrics() -> None:
    rows = [
        _t(pnl=10.0, closed=True),  # win
        _t(pnl=20.0, closed=True),  # win
        _t(pnl=-5.0, closed=True),  # loss
        _t(closed=False),  # still open
    ]
    s = summarize_trades(rows)
    assert s.total == 4
    assert s.closed == 3
    assert s.open_trades == 1
    assert s.wins == 2
    assert s.losses == 1
    assert s.win_rate == 2 / 3
    assert s.gross_profit == 30.0
    assert s.gross_loss == -5.0
    assert s.net_pnl == 25.0
    assert s.profit_factor == 30.0 / 5.0
    assert s.avg_win == 15.0
    assert s.avg_loss == -5.0


def test_summarize_trades_profit_factor_undefined_without_losses() -> None:
    s = summarize_trades([_t(pnl=10.0, closed=True), _t(pnl=5.0, closed=True)])
    assert s.profit_factor is None  # no losses → undefined
    assert s.gross_loss == 0.0
    assert s.win_rate == 1.0


def test_summarize_trades_empty() -> None:
    s = summarize_trades([])
    assert s.total == 0
    assert s.closed == 0
    assert s.open_trades == 0
    assert s.win_rate == 0.0
    assert s.profit_factor is None
