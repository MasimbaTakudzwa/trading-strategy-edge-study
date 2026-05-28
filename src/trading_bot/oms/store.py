"""Run persistence — the durable record of what a live/paper loop did.

A *run* is one session of the bot trading an instrument with a strategy. Every
order it places, every account snapshot it takes, and notable events all hang
off a run_id so the `report`/`status` commands can slice history by env.

The engine depends on the `RunStore` Protocol, not the concrete DB class, so
tests inject a fake that captures calls (same pattern as the Broker protocol).
`DbRunStore` is the real implementation backed by Postgres.

Order writes are idempotent: re-recording the same client_order_id (e.g. a bar
re-processed after a restart) is a no-op, never a duplicate row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from trading_bot.data.db import session_scope
from trading_bot.data.models import account_snapshots, events, orders, runs, trades
from trading_bot.execution.base import AccountSnapshot, OrderRequest, OrderResult, Side
from trading_bot.observability.logging import get_logger

log = get_logger(__name__)


class RunStore(Protocol):
    """Persistence interface the paper/live engine writes through."""

    def start_run(
        self,
        *,
        env: str,
        strategy: str,
        params: dict[str, Any],
        starting_balance: float,
        notes: str | None = None,
    ) -> str:
        """Create a run row and return its id."""
        ...

    def end_run(self, run_id: str) -> None:
        """Mark a run finished (sets ended_at)."""
        ...

    def record_order(
        self,
        *,
        run_id: str,
        env: str,
        request: OrderRequest,
        result: OrderResult | None,
    ) -> int | None:
        """Persist a placed order (idempotent on client_order_id).

        Returns the new order id, or None if the order already existed (a
        re-processed bar) — the caller uses this to avoid double-recording the
        matching trade.
        """
        ...

    def record_trade(
        self,
        *,
        run_id: str,
        env: str,
        instrument: str,
        side: str,
        units: float,
        entry_price: float,
        entry_time: datetime,
        entry_order_id: int | None = None,
    ) -> int:
        """Open a trade row (entry side; exit fields filled in on close)."""
        ...

    def close_open_trades(
        self,
        *,
        run_id: str,
        instrument: str,
        exit_price: float,
        exit_time: datetime,
        value_per_point: float = 1.0,
    ) -> int:
        """Settle every open trade for an instrument: set exit + realized P&L.
        Returns the number closed (0 if already flat — idempotent)."""
        ...

    def record_snapshot(self, *, run_id: str, env: str, snapshot: AccountSnapshot) -> None:
        """Persist a point-in-time account snapshot."""
        ...

    def record_event(
        self,
        *,
        run_id: str | None,
        env: str,
        level: str,
        category: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Persist a notable event for the audit trail."""
        ...


class DbRunStore:
    """Postgres-backed RunStore."""

    def start_run(
        self,
        *,
        env: str,
        strategy: str,
        params: dict[str, Any],
        starting_balance: float,
        notes: str | None = None,
    ) -> str:
        stmt = (
            insert(runs)
            .values(
                env=env,
                strategy=strategy,
                params=params,
                starting_balance=starting_balance,
                notes=notes,
            )
            .returning(runs.c.id)
        )
        with session_scope() as session:
            run_id = session.execute(stmt).scalar_one()
        log.info("run_started", run_id=str(run_id), env=env, strategy=strategy)
        return str(run_id)

    def end_run(self, run_id: str) -> None:
        from sqlalchemy import func

        stmt = (
            update(runs)
            .where(runs.c.id == uuid.UUID(run_id))
            .values(ended_at=func.now())
        )
        with session_scope() as session:
            session.execute(stmt)
        log.info("run_ended", run_id=run_id)

    def record_order(
        self,
        *,
        run_id: str,
        env: str,
        request: OrderRequest,
        result: OrderResult | None,
    ) -> int | None:
        values: dict[str, Any] = {
            "run_id": uuid.UUID(run_id),
            "env": env,
            "client_order_id": request.client_order_id,
            "instrument": request.instrument,
            "side": request.side.value,
            "units": request.units,
            "order_type": request.order_type.value,
            "limit_price": request.limit_price,
            "stop_loss_price": request.stop_loss_price,
            "take_profit_price": request.take_profit_price,
            "status": result.status if result else "pending",
        }
        if result is not None:
            values.update(
                broker_order_id=result.broker_order_id,
                filled_at=result.filled_at,
                filled_price=result.filled_price,
                rejection_reason=result.rejection_reason,
                raw_response=result.raw_response,
            )
        # Idempotent: a re-processed bar yields the same client_order_id; skip it.
        # RETURNING gives the new id, or nothing on conflict (→ None).
        stmt = (
            pg_insert(orders)
            .values(values)
            .on_conflict_do_nothing(index_elements=["client_order_id"])
            .returning(orders.c.id)
        )
        with session_scope() as session:
            order_id = session.execute(stmt).scalar_one_or_none()
        log.info(
            "order_recorded",
            run_id=run_id,
            client_order_id=request.client_order_id,
            status=values["status"],
            order_id=order_id,
        )
        return int(order_id) if order_id is not None else None

    def record_trade(
        self,
        *,
        run_id: str,
        env: str,
        instrument: str,
        side: str,
        units: float,
        entry_price: float,
        entry_time: datetime,
        entry_order_id: int | None = None,
    ) -> int:
        stmt = (
            insert(trades)
            .values(
                run_id=uuid.UUID(run_id),
                env=env,
                instrument=instrument,
                side=side,
                units=units,
                entry_price=entry_price,
                entry_time=entry_time,
                entry_order_id=entry_order_id,
                closed=False,
            )
            .returning(trades.c.id)
        )
        with session_scope() as session:
            trade_id = session.execute(stmt).scalar_one()
        log.info("trade_opened", run_id=run_id, instrument=instrument, side=side, units=units)
        return int(trade_id)

    def close_open_trades(
        self,
        *,
        run_id: str,
        instrument: str,
        exit_price: float,
        exit_time: datetime,
        value_per_point: float = 1.0,
    ) -> int:
        select_open = (
            select(trades.c.id, trades.c.side, trades.c.units, trades.c.entry_price)
            .where(trades.c.run_id == uuid.UUID(run_id))
            .where(trades.c.instrument == instrument)
            .where(trades.c.closed.is_(False))
        )
        closed = 0
        with session_scope() as session:
            for row in session.execute(select_open).all():
                signed = float(row.units) if row.side == Side.BUY.value else -float(row.units)
                realized = (exit_price - float(row.entry_price)) * signed * value_per_point
                session.execute(
                    update(trades)
                    .where(trades.c.id == row.id)
                    .values(
                        exit_price=exit_price,
                        exit_time=exit_time,
                        realized_pnl=realized,
                        closed=True,
                    )
                )
                closed += 1
        log.info("trades_closed", run_id=run_id, instrument=instrument, closed=closed)
        return closed

    def record_snapshot(self, *, run_id: str, env: str, snapshot: AccountSnapshot) -> None:
        stmt = insert(account_snapshots).values(
            run_id=uuid.UUID(run_id),
            env=env,
            balance=snapshot.balance,
            equity=snapshot.equity,
            unrealized_pnl=snapshot.unrealized_pnl,
            margin_used=snapshot.margin_used,
            open_positions=snapshot.open_positions,
        )
        with session_scope() as session:
            session.execute(stmt)

    def record_event(
        self,
        *,
        run_id: str | None,
        env: str,
        level: str,
        category: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        stmt = insert(events).values(
            run_id=uuid.UUID(run_id) if run_id else None,
            env=env,
            level=level,
            category=category,
            message=message,
            context=context,
        )
        with session_scope() as session:
            session.execute(stmt)
