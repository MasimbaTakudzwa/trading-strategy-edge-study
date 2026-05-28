-- Trading bot schema. Auto-applied by Postgres on first container start
-- (mounted in /docker-entrypoint-initdb.d), and idempotent so re-runs are safe.
--
-- Every order/trade/snapshot is tagged with `env` so practice and live
-- performance can be sliced and compared.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- runs: one row per bot invocation (paper, live, or backtest)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    env              TEXT NOT NULL CHECK (env IN ('practice', 'live', 'backtest')),
    strategy         TEXT NOT NULL,
    params           JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at         TIMESTAMPTZ,
    starting_balance NUMERIC,
    notes            TEXT
);
CREATE INDEX IF NOT EXISTS runs_env_started_idx ON runs (env, started_at DESC);

-- ---------------------------------------------------------------------------
-- candles: OHLCV time-series, TimescaleDB hypertable
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candles (
    instrument  TEXT NOT NULL,
    granularity TEXT NOT NULL,         -- M1, M5, H1, H4, D, ...
    ts          TIMESTAMPTZ NOT NULL,
    open        NUMERIC NOT NULL,
    high        NUMERIC NOT NULL,
    low         NUMERIC NOT NULL,
    close       NUMERIC NOT NULL,
    volume      INTEGER,
    PRIMARY KEY (instrument, granularity, ts)
);
SELECT create_hypertable('candles', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS candles_instrument_idx ON candles (instrument, granularity, ts DESC);

-- ---------------------------------------------------------------------------
-- orders: every order request the OMS sends, with idempotency key
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id                BIGSERIAL PRIMARY KEY,
    run_id            UUID NOT NULL REFERENCES runs(id),
    env               TEXT NOT NULL CHECK (env IN ('practice', 'live', 'backtest')),
    client_order_id   TEXT UNIQUE NOT NULL,  -- our idempotency key, sent to broker
    broker_order_id   TEXT,
    instrument        TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    units             NUMERIC NOT NULL,
    order_type        TEXT NOT NULL,         -- market, limit, stop
    limit_price       NUMERIC,
    stop_loss_price   NUMERIC,
    take_profit_price NUMERIC,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at      TIMESTAMPTZ,
    filled_at         TIMESTAMPTZ,
    filled_price      NUMERIC,
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending, submitted, filled, rejected, cancelled
    rejection_reason  TEXT,
    raw_response      JSONB
);
CREATE INDEX IF NOT EXISTS orders_run_idx ON orders (run_id);
CREATE INDEX IF NOT EXISTS orders_env_requested_idx ON orders (env, requested_at DESC);

-- ---------------------------------------------------------------------------
-- trades: completed round-trips (one entry + one exit), with realised P&L
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES runs(id),
    env             TEXT NOT NULL CHECK (env IN ('practice', 'live', 'backtest')),
    instrument      TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    units           NUMERIC NOT NULL,
    entry_price     NUMERIC NOT NULL,
    exit_price      NUMERIC,
    entry_time      TIMESTAMPTZ NOT NULL,
    exit_time       TIMESTAMPTZ,
    realized_pnl    NUMERIC,
    fees            NUMERIC NOT NULL DEFAULT 0,
    swap            NUMERIC NOT NULL DEFAULT 0,  -- overnight financing
    entry_order_id  BIGINT REFERENCES orders(id),
    exit_order_id   BIGINT REFERENCES orders(id),
    closed          BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS trades_run_idx ON trades (run_id);
CREATE INDEX IF NOT EXISTS trades_env_entry_idx ON trades (env, entry_time DESC);
CREATE INDEX IF NOT EXISTS trades_open_idx ON trades (instrument) WHERE closed = FALSE;

-- ---------------------------------------------------------------------------
-- account_snapshots: periodic snapshots of broker account state
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES runs(id),
    env             TEXT NOT NULL CHECK (env IN ('practice', 'live', 'backtest')),
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    balance         NUMERIC NOT NULL,
    equity          NUMERIC NOT NULL,
    unrealized_pnl  NUMERIC,
    margin_used     NUMERIC,
    open_positions  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS snapshots_run_ts_idx ON account_snapshots (run_id, ts DESC);
-- Not a hypertable: low write volume (~1 row / 30s) and a surrogate id PK that
-- doesn't include the ts partitioning column. A plain indexed table is plenty
-- here. Only candles (high-frequency) needs hypertable partitioning.

-- ---------------------------------------------------------------------------
-- events: bot lifecycle, errors, alerts — append-only audit log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id        BIGSERIAL PRIMARY KEY,
    run_id    UUID REFERENCES runs(id),
    env       TEXT NOT NULL CHECK (env IN ('practice', 'live', 'backtest')),
    ts        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    level     TEXT NOT NULL,         -- info, warning, error, critical
    category  TEXT NOT NULL,         -- startup, signal, order, fill, kill_switch, etc.
    message   TEXT NOT NULL,
    context   JSONB
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts DESC);
CREATE INDEX IF NOT EXISTS events_level_idx ON events (level, ts DESC) WHERE level IN ('error', 'critical');
