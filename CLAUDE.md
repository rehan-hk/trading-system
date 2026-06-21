# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An algorithmic options trading system for Indian equity indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY) using the Kite Connect API (Zerodha). The strategy sells strangles/straddles on index options, entering positions at specific times and exiting when a profit/loss target (in points) is reached.

## Setup

```bash
pip install -e ".[dev]"       # install package + test deps
cp .env.example .env          # then fill in credentials
```

All config is read from environment variables / `.env` via `config/settings.py` (Pydantic Settings). No more `api_key.txt`.

## Commands

```bash
# Run the live trading engine
python -m option_trading.main

# Run the offline market simulation (no Kite connection needed)
python scripts/simulate.py

# Run all tests
pytest tests/

# Run a single test file
pytest tests/unit/test_strategy_engine.py -v

# Run with coverage
pytest tests/ --cov=src/option_trading
```

## Architecture

The codebase is a Python package under `src/option_trading/` with separated concerns:

```
config/settings.py          Pydantic Settings — all config from env vars
src/option_trading/
  main.py                   Entry point: wire up all components, start WebSocket
  models/                   Pure data — no I/O, no business logic
    strategy.py             Strategy dataclass, StrategyResult/TradeMode enums
    trade.py                TradeRecord dataclass
    market.py               MarketTick, MarketDepth, OrderSide
  utils/                    Stateless helpers
    price.py                round_to_nearest_fifty/hundred, round_price_based_on_template
    time_utils.py           parse_time, time_in_range, add/subtract_seconds_from_time
    circuit_breaker.py      Three-state CircuitBreaker (CLOSED→OPEN→HALF_OPEN)
    logging_setup.py        Structured JSON logging with correlation_id (contextvars)
  services/                 Stateless business logic
    expiry.py               get_expiry_dates (all 4 indices), format_date
    instruments.py          get_tokens — weekly expiry with monthly fallback (EOM logic)
    liquidity.py            check_liquidity via depth bid/ask quantities and spread
    risk.py                 check_success_rate (binomial), calculate_drawdown
    strategy_selector.py    get_strategies_to_run_today from GROUPED_tier_1_results.csv
    notifications.py        send_alert via boto3 SNS
  core/                     Stateful orchestration
    auth.py                 autologin — Selenium headless Chrome Kite login
    broker.py               BrokerClient — place_order with retry + circuit breaker
    strategy_engine.py      StrategyEngine.process_ticks() — full strategy state machine
    websocket_manager.py    WebSocketManager — wraps KiteTicker, delegates to engine
  persistence/              I/O boundary
    dynamo_state.py         DynamoStateStore — strategy state to DynamoDB (7-day TTL)
    trade_logger.py         TradeLogger — accumulate TradeRecords, save CSV + DynamoDB
    s3_archiver.py          S3Archiver — archive daily CSVs to S3
```

### Strategy Lifecycle (StrategyEngine.process_ticks)

Each strategy dict is keyed by entry time string (`"HH:MM:SS"`) and goes through these states:

1. **Pending** — waiting; token lookup + WebSocket subscription happen at entry time (fresh spot price)
2. **Entry window** (±15s tolerance) — if `tradable=True`, set `running=True`; if `tradable=False`, skip with reason "DTE not 0"
3. **Liquidity check** — on the first tick after entry: check bid/ask depth. Skip if insufficient
4. **Entry** — first tick with both CE+PE prices → record `initial_sum`, place SELL orders (real) or log (dummy). Partial fills (only one leg) are immediately reversed
5. **Running** — each tick: compute `price_diff = current_sum - initial_sum`
   - Stop-loss: `points` if in profit (diff<0) or if `second==0`; `1.0 × points` if in loss
6. **Exit** — `abs(price_diff) >= stop_loss` → place BUY orders (real) or log (dummy); record `result = profit|loss`
7. **Market close** (≥15:30) — square off all running real strategies, log dummy strategies, close WebSocket
8. **Conditional strategies** — `conditional_on` key: only enter/execute if the referenced strategy's `result == 'profit'`

### Instrument Token Resolution (services/instruments.py)

`get_tokens(kite, instruments_df, index, strangle)` implements two-tier expiry logic:
- **Primary**: weekly expiry (`expiry_1` from `get_expiry_dates`), formatted as `YY{M}DD` (no leading zero on month)
- **Fallback**: monthly expiry (`{YY}{MON}` e.g. `24JUN`), only attempted in the last 2 weeks of the month

### Index Constants

| Index | Symbol | Expiry Day | Strike Rounding | Lot Size | Margin Required |
|---|---|---|---|---|---|
| `NSE:NIFTY 50` | `NIFTY` | Thursday | 50 | 25 | ₹2,20,000 |
| `NSE:NIFTY BANK` | `BANKNIFTY` | Wednesday | 100 | 15 | ₹2,20,000 |
| `NSE:NIFTY FIN SERVICE` | `FINNIFTY` | Tuesday | 50 | 40 | ₹2,80,000 |
| `NSE:NIFTY MID SELECT` | `MIDCPNIFTY` | Monday | 100 | 50 | ₹2,00,000 |

### Circuit Breaker (utils/circuit_breaker.py)

`CircuitBreaker(failure_threshold=3, reset_timeout=30)` — wraps any callable. After 3 consecutive failures it opens and raises `CircuitBreakerOpen` without calling the function. After 30s cooldown it enters HALF_OPEN and allows one probe call through.

### Logging

All logs are structured JSON via `utils/logging_setup.py`. Each log record includes `correlation_id` (set via `contextvars` at strategy entry) linking all log lines for a single strategy execution.

### Infrastructure (deploy/aws/)

- `cloudformation.yml` — DynamoDB (2 tables with TTL), S3, SNS, CloudWatch log group, IAM task role
- `task-definition.json` — ECS Fargate task (1 vCPU, 2 GB, Secrets Manager for credentials, health check on `:8080`)
- EventBridge rules: start task at 03:45 UTC (09:15 IST), stop at 10:15 UTC (15:45 IST)

### Local Development with Docker

```bash
docker-compose up   # starts trading engine + LocalStack (DynamoDB, S3, SNS)
```

### Legacy Files

The original monolithic scripts (`KITE_TRADING_STRATEGY.py`, `trade_executor_v2/v4/v5.py`, `market_simulation.py`, `connect.py`) remain in the root for reference. The new package fully supersedes them.
