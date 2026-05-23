# trading-bot

FX trading bot. Technical rules-based strategies. cTrader Open API + FP Markets.

## What this is

A bot that runs technical strategies against the cTrader platform via Spotware's Open API. Designed for phased rollout:

1. **Backtest** strategies on historical data
2. **Paper trade** on a cTrader demo account (free, real prices, fake money)
3. **Live trade** on a real account with tiny capital
4. **Scale** only after the demo and tiny-live phases produce clean results

Switching between demo and live is a single env var (`CTRADER_ENV=demo|live`). Every order, trade, and account snapshot is logged to Postgres with the env tagged, so you can compare demo and live performance side by side.

## Why this stack

| | Why |
|---|---|
| **FP Markets** | One of the few brokers that accepts Zimbabwean clients AND supports cTrader. Free demo accounts. |
| **cTrader Open API** | Free forever (no SaaS middleman like MetaApi), official from Spotware, language-neutral, native protobuf over TLS. |
| **TimescaleDB** | Time-series-aware Postgres extension. Same SQL, hypertables for candles + snapshots. |

## Setup

### 1. Sign up for an FP Markets demo account

<https://www.fpmarkets.com/en-zw/> → open a demo account. Free, instant, no KYC needed for demo. Note your **cTID account number** (e.g. `1234567`) — you'll need it.

### 2. Register a cTrader Open API application

<https://openapi.ctrader.com> → log in with your cTrader credentials → create a new application. Note:
- `Client ID`
- `Client Secret`

These identify *your app* to cTrader. They're separate from your account.

### 3. Authorise the app for your account (OAuth)

Still on openapi.ctrader.com, run the OAuth flow to grant your app access to your FP Markets demo account. This produces:
- `Access Token` — used to authenticate API calls
- `Refresh Token` — used to refresh expired access tokens
- `ctidTraderAccountId` (numeric) — the account ID cTrader's API uses internally (different from the human-readable account number)

Spotware's full walkthrough: <https://help.ctrader.com/open-api/>

### 4. Configure the bot

```bash
cp .env.example .env
```

Edit `.env`:

```
CTRADER_ENV=demo
CTRADER_CLIENT_ID=<from step 2>
CTRADER_CLIENT_SECRET=<from step 2>
CTRADER_ACCOUNT_ID=<numeric ctidTraderAccountId from step 3>
CTRADER_ACCESS_TOKEN=<from step 3>
CTRADER_REFRESH_TOKEN=<from step 3>
```

### 5. Start infrastructure and install

```bash
make install      
make up           
make db-init      
make test         
```

### 6. Verify the connection

```bash
uv run tbot ctrader symbols --filter EUR
```

If your credentials are right, this prints all available instruments matching "EUR" with their numeric IDs. This is also how you find the exact instrument name to pass to `tbot fetch` — broker naming varies (some use `EURUSD`, others `EUR/USD`).

## CLI

```
tbot db init                       # create schema
tbot db status                     # show stored candle counts and date ranges
tbot ctrader symbols [--filter X]  # list available instruments + IDs
tbot fetch EURUSD H1 --since 2020-01-01
tbot fetch EURUSD H1 --resume      # incremental — only new bars
tbot backtest --strategy donchian  # run backtest on stored data (week 2)
tbot paper --strategy donchian --instrument EURUSD   # demo (week 4)
tbot live  --strategy donchian --instrument EURUSD   # live, prompts (week 10)
tbot status                        # current positions, today's P&L (week 4)
tbot reconcile                     # broker positions vs DB (week 4)
tbot report --env practice         # performance summary (week 2)
```

## Performance tracking on the demo account

Every paper trade is logged to the same tables as live trades, with `env='practice'`. The `tbot report --env practice` command summarises performance over a time range — P&L, win rate, drawdown — so you can decide when (or whether) to graduate to live.

## Layout

```
src/trading_bot/
├── config.py              pydantic settings, demo/live switch
├── data/
│   ├── ctrader_protocol.py    sync facade over Twisted-based SDK (crochet)
│   ├── ctrader_fetcher.py     ProtoOAGetTrendbarsReq with pagination + symbol cache
│   ├── candles.py             idempotent upsert + read helpers
│   ├── models.py              SQLAlchemy Core tables
│   └── db.py                  engine + session helpers
├── strategies/                one file per strategy
├── backtest/                  vectorbt harness (week 2)
├── risk/                      sizing, limits, kill switch
├── execution/
│   ├── base.py                Broker protocol + Intent / OrderRequest types
│   └── ctrader_broker.py      cTrader Open API implementation (week 4)
├── oms/                       order state machine (week 4)
├── observability/             structlog setup
└── cli.py                     typer subcommands

ops/
├── sql/                       schema migrations
├── grafana/                   dashboards (later)
└── systemd/                   service files for VPS deployment (later)
```

## Design rule

Strategy code never imports cTrader, OANDA, or any broker module. The flow is:

```
Strategy → emits Intent → Risk gate validates + sizes → OMS makes OrderRequest → Broker adapter sends
```

`execution/base.py` defines the `Broker` Protocol. cTrader is one implementation. Swapping brokers later (e.g. if FP Markets falls through) means writing a new adapter — no changes to strategy, risk, or OMS code.

## Status

- ✅ Week 1: data fetcher + candle storage (cTrader Open API)
- [ ] Week 2: Donchian breakout backtest
- [ ] Week 3: risk module + reconciliation
- [ ] Week 4: cTrader execution adapter + OMS
- [ ] Week 5: end-to-end paper trading
- [ ] Week 6-9: paper trading and bug fixes
- [ ] Week 10: live with €500
