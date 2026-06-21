# Option Trading Engine

Autonomous options trading system for Indian indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY) on Zerodha Kite Connect. Sells strangles/straddles at configured times and exits when a profit/loss target is hit.

**Fully autonomous** — logs into Kite every morning via headless Chrome + TOTP, runs all strategies in parallel threads, squares off at market close, then shuts down. No manual steps.

---

## How it works

1. ECS task starts at 09:15 IST (EventBridge scheduled)
2. Auto-login to Kite Connect (Selenium + pyotp TOTP)
3. Each strategy runs on its own thread, waiting for its entry time
4. On entry: place SELL orders for CE + PE (or log in dummy mode)
5. On each tick: check if `|current_sum - entry_sum| >= points` → exit
6. At 15:30: square off any open positions, save trade log to S3 + DynamoDB
7. Task stops at 15:45 IST

---

## Run locally (no Kite needed)

```bash
pip install -e ".[dev]"
python scripts/simulate.py
```

Simulates 09:15–12:00 with synthetic random ticks and prints a full trade log.

---

## Run for real

**1. Copy and fill credentials**
```bash
cp .env.example .env
# fill in API_KEY, API_SECRET, KITE_USERNAME, KITE_PASSWORD, TOTP_SECRET
# set TRADE_MODE=dummy to paper trade, TRADE_MODE=real to place live orders
```

**2. Run**
```bash
python -m option_trading.main
```

---

## Deploy to AWS

CloudFormation provisions DynamoDB, S3, SNS, CloudWatch, and IAM roles:
```bash
aws cloudformation deploy \
  --template-file deploy/aws/cloudformation.yml \
  --stack-name trading-engine \
  --capabilities CAPABILITY_NAMED_IAM
```

Then store credentials in Secrets Manager, push a Docker image to ECR, and run the ECS task. See [`architecture.md`](architecture.md) for the full setup guide.

---

## Project layout

```
config/settings.py          All config via env vars / .env (Pydantic)
src/option_trading/
  main.py                   Entry point
  core/strategy_engine.py   Parallel strategy workers + tick fanout
  core/auth.py              Headless Kite login (Selenium + TOTP)
  core/broker.py            Order placement with retry + circuit breaker
  services/                 Instrument tokens, liquidity, strategy selection
  persistence/              DynamoDB state, S3 archiver, trade logger
scripts/simulate.py         Offline simulation — no Kite needed
deploy/aws/                 CloudFormation + ECS task definition
tests/                      91 unit + integration tests
```

---

## Key config

| Variable | Default | Description |
|---|---|---|
| `TRADE_MODE` | `dummy` | `dummy` logs trades, `real` places live orders |
| `N_STRATEGIES` | `3` | Number of strategies to run per day |
| `MARKET_CLOSE_TIME` | `15:30:00` | Square-off time |
| `TRADE_MARGIN` | `1000000` | Total margin budget across all strategies |
