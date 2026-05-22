# trading-bot

FX/futures trading bot. Technical rules-based strategies, custom Python build.

## What this is

A bot that runs technical strategies against OANDA's API. Designed for phased rollout:

1. **Backtest** strategies on historical data
2. **Paper trade** on OANDA's practice environment (free, real prices, fake money)
3. **Live trade** on a real OANDA account with tiny capital
4. **Scale** only after the practice and tiny-live phases produce clean results

Switching between practice and live is a single env var (`OANDA_ENV=practice|live`). Every order, trade, and account snapshot is logged to Postgres with the env tagged, so you can compare practice and live performance side by side.

## Quick start

Prereqs: Docker, Python 3.12, [uv](https://docs.astral.sh/uv/) (`make install` will install it for you if missing).

```bash
# 1. Set up env
cp .env.example .env
# Edit .env: add your OANDA practice account ID and API token
#   Free practice account: https://www.oanda.com/demo-account/
#   Token: https://www.oanda.com/demo-account/tpa/personal_token

# 2. Install deps
make install

# 3. Start Postgres + Redis
make up

# 4. Initialise the DB schema
make db-init

# 5. Run the test suite
make test

# 6. (Once strategies are wired) start paper trading
make paper
```

## CLI

The `tbot` command is the entrypoint:

```
tbot db init                # create schema
tbot fetch EUR_USD H1       # backfill historical candles
tbot backtest donchian      # run backtest on stored data
tbot paper --strategy donchian --instrument EUR_USD   # run on practice account
tbot live --strategy donchian --instrument EUR_USD    # run on live (prompts for confirmation)
tbot status                 # current positions, today's P&L
tbot reconcile              # broker positions vs DB — should always match
tbot report --env practice  # performance summary for practice trading
```

## Practice account tracking

Sign up at <https://www.oanda.com/demo-account/> — it's free and gives you a fake $100k. Set `OANDA_ENV=practice` in `.env` and everything routes to `api-fxpractice.oanda.com`.

Every paper trade is logged to the same tables as live trades, with `env='practice'`. The `tbot report --env practice` command summarises performance over a time range so you can decide when (or whether) to graduate to live.

## Layout

```
src/trading_bot/
├── config.py          # pydantic settings, practice/live switch
├── data/              # OANDA fetchers, DB models, candle storage
├── strategies/        # one file per strategy, all implement Strategy interface
├── backtest/          # vectorbt harness + walk-forward runner
├── risk/              # sizing, limits, kill switch — gates every order
├── execution/         # broker adapter interface + OANDA impl
├── oms/               # order state machine, idempotency, reconciliation
├── observability/     # structlog, metrics, alerts
└── cli.py             # typer subcommands

ops/
├── sql/               # schema migrations
├── grafana/           # dashboards (later)
└── systemd/           # service files for VPS deployment (later)
```

## Design rule

Strategy code never touches broker code directly. Flow is:

```
Strategy → emits Intent → Risk gate validates → OMS translates to Order → Broker adapter sends
```

Each layer is testable in isolation. The risk gate is the only layer that can refuse an order — strategies always go through it, including in backtest.

## Status

Scaffold complete. Next milestones:

- [ ] Week 1: data fetcher + candle storage
- [ ] Week 2: Donchian breakout backtest
- [ ] Week 3: risk module + reconciliation
- [ ] Week 4: OANDA execution adapter + OMS
- [ ] Week 5: end-to-end paper trading
- [ ] Week 6-9: paper trading and bug fixes
- [ ] Week 10: live with €500
