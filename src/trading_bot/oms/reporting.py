"""Read-side queries + pure aggregators for the observability commands.

Writes live in oms/store.py; this module reads them back for `tbot status`,
`reconcile`, and `report`. The pure functions (`summarize_trades`,
`net_positions`) are deliberately split from the DB queries so the reporting
maths is unit-testable without a database.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from trading_bot.data.db import session_scope
from trading_bot.data.models import account_snapshots, trades
from trading_bot.execution.base import Side

_TOL = 1e-9


@dataclass(frozen=True)
class TradeRecord:
    """A trade row as the reporting layer sees it (read model)."""

    instrument: str
    side: str
    units: float
    entry_price: float
    exit_price: float | None
    realized_pnl: float | None
    closed: bool


@dataclass(frozen=True)
class PerformanceSummary:
    total: int
    closed: int
    open_trades: int
    wins: int
    losses: int
    win_rate: float  # fraction of settled trades that won
    gross_profit: float
    gross_loss: float  # <= 0
    net_pnl: float
    profit_factor: float | None  # None when there are no losses (undefined)
    avg_win: float
    avg_loss: float


@dataclass(frozen=True)
class AccountState:
    balance: float
    equity: float
    open_positions: int
    ts: datetime | None


def _signed_units(side: str, units: float) -> float:
    return units if side == Side.BUY.value else -units


def net_positions(rows: Iterable[TradeRecord]) -> dict[str, float]:
    """Net signed units per instrument across OPEN trades only. Pure.

    Flat instruments are omitted. This is the DB's view of what's open, for
    reconciliation against the broker's truth.
    """
    out: dict[str, float] = {}
    for r in rows:
        if r.closed:
            continue
        out[r.instrument] = out.get(r.instrument, 0.0) + _signed_units(r.side, r.units)
    return {k: v for k, v in out.items() if abs(v) > _TOL}


def summarize_trades(rows: Sequence[TradeRecord]) -> PerformanceSummary:
    """Aggregate trade rows into headline performance metrics. Pure."""
    open_rows = [r for r in rows if not r.closed]
    settled = [r for r in rows if r.closed and r.realized_pnl is not None]
    wins = [r for r in settled if (r.realized_pnl or 0.0) > 0]
    losses = [r for r in settled if (r.realized_pnl or 0.0) < 0]

    gross_profit = sum((r.realized_pnl or 0.0) for r in wins)
    gross_loss = sum((r.realized_pnl or 0.0) for r in losses)  # <= 0
    net = gross_profit + gross_loss

    return PerformanceSummary(
        total=len(rows),
        closed=len(settled),
        open_trades=len(open_rows),
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / len(settled)) if settled else 0.0,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_pnl=net,
        profit_factor=(gross_profit / abs(gross_loss)) if gross_loss != 0 else None,
        avg_win=(gross_profit / len(wins)) if wins else 0.0,
        avg_loss=(gross_loss / len(losses)) if losses else 0.0,
    )


# -- DB readers (thin wrappers over the pure functions above) ----------------


def _load_trade_rows(env: str, since: datetime | None = None) -> list[TradeRecord]:
    stmt = select(
        trades.c.instrument,
        trades.c.side,
        trades.c.units,
        trades.c.entry_price,
        trades.c.exit_price,
        trades.c.realized_pnl,
        trades.c.closed,
    ).where(trades.c.env == env)
    if since is not None:
        stmt = stmt.where(trades.c.entry_time >= since)
    with session_scope() as session:
        rows = session.execute(stmt).all()
    return [
        TradeRecord(
            instrument=r.instrument,
            side=r.side,
            units=float(r.units),
            entry_price=float(r.entry_price),
            exit_price=float(r.exit_price) if r.exit_price is not None else None,
            realized_pnl=float(r.realized_pnl) if r.realized_pnl is not None else None,
            closed=r.closed,
        )
        for r in rows
    ]


def performance(env: str = "practice", days: int = 30) -> PerformanceSummary:
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    return summarize_trades(_load_trade_rows(env, since))


def open_positions(env: str = "practice") -> dict[str, float]:
    """DB view of currently-open positions (instrument → signed units)."""
    return net_positions(_load_trade_rows(env))


def latest_account_state(env: str = "practice") -> AccountState | None:
    stmt = (
        select(
            account_snapshots.c.balance,
            account_snapshots.c.equity,
            account_snapshots.c.open_positions,
            account_snapshots.c.ts,
        )
        .where(account_snapshots.c.env == env)
        .order_by(account_snapshots.c.ts.desc())
        .limit(1)
    )
    with session_scope() as session:
        row = session.execute(stmt).first()
    if row is None:
        return None
    return AccountState(
        balance=float(row.balance),
        equity=float(row.equity),
        open_positions=row.open_positions,
        ts=row.ts,
    )


def todays_pnl_pct(env: str = "practice") -> float | None:
    """Equity change since the first snapshot of the current UTC day, or None
    if there's nothing to compare."""
    start_of_day = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    base = (
        select(account_snapshots.c.equity)
        .where(account_snapshots.c.env == env)
        .where(account_snapshots.c.ts >= start_of_day)
    )
    with session_scope() as session:
        first = session.execute(base.order_by(account_snapshots.c.ts.asc()).limit(1)).scalar_one_or_none()
        last = session.execute(base.order_by(account_snapshots.c.ts.desc()).limit(1)).scalar_one_or_none()
    if first is None or last is None or float(first) == 0:
        return None
    return (float(last) - float(first)) / float(first)
