"""SQLAlchemy Core table definitions.

The schema source of truth lives in ops/sql/001_init.sql — these definitions
exist so Python code can do typed inserts and queries against those tables.
Keep them in sync when the SQL changes.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()


candles = Table(
    "candles",
    metadata,
    Column("instrument", String, nullable=False),
    Column("granularity", String, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("open", Numeric, nullable=False),
    Column("high", Numeric, nullable=False),
    Column("low", Numeric, nullable=False),
    Column("close", Numeric, nullable=False),
    Column("volume", Integer),
    PrimaryKeyConstraint("instrument", "granularity", "ts"),
    Index("candles_instrument_idx", "instrument", "granularity", "ts"),
)


runs = Table(
    "runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("env", String, nullable=False),
    Column("strategy", String, nullable=False),
    Column("params", JSONB, nullable=False, server_default="{}"),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("ended_at", DateTime(timezone=True)),
    Column("starting_balance", Numeric),
    Column("notes", Text),
    CheckConstraint("env IN ('practice', 'live', 'backtest')", name="runs_env_check"),
)


orders = Table(
    "orders",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False),
    Column("env", String, nullable=False),
    Column("client_order_id", String, nullable=False),
    Column("broker_order_id", String),
    Column("instrument", String, nullable=False),
    Column("side", String, nullable=False),
    Column("units", Numeric, nullable=False),
    Column("order_type", String, nullable=False),
    Column("limit_price", Numeric),
    Column("stop_loss_price", Numeric),
    Column("take_profit_price", Numeric),
    Column("requested_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("submitted_at", DateTime(timezone=True)),
    Column("filled_at", DateTime(timezone=True)),
    Column("filled_price", Numeric),
    Column("status", String, nullable=False, server_default="pending"),
    Column("rejection_reason", Text),
    Column("raw_response", JSONB),
    UniqueConstraint("client_order_id", name="orders_client_order_id_key"),
)


trades = Table(
    "trades",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False),
    Column("env", String, nullable=False),
    Column("instrument", String, nullable=False),
    Column("side", String, nullable=False),
    Column("units", Numeric, nullable=False),
    Column("entry_price", Numeric, nullable=False),
    Column("exit_price", Numeric),
    Column("entry_time", DateTime(timezone=True), nullable=False),
    Column("exit_time", DateTime(timezone=True)),
    Column("realized_pnl", Numeric),
    Column("fees", Numeric, nullable=False, server_default="0"),
    Column("swap", Numeric, nullable=False, server_default="0"),
    Column("entry_order_id", BigInteger, ForeignKey("orders.id")),
    Column("exit_order_id", BigInteger, ForeignKey("orders.id")),
    Column("closed", Boolean, nullable=False, server_default="false"),
)


account_snapshots = Table(
    "account_snapshots",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False),
    Column("env", String, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("balance", Numeric, nullable=False),
    Column("equity", Numeric, nullable=False),
    Column("unrealized_pnl", Numeric),
    Column("margin_used", Numeric),
    Column("open_positions", Integer, nullable=False, server_default="0"),
)


events = Table(
    "events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("runs.id")),
    Column("env", String, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("level", String, nullable=False),
    Column("category", String, nullable=False),
    Column("message", Text, nullable=False),
    Column("context", JSONB),
)
