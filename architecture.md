# Architecture

## How the Options Trading System Works

End-to-end walkthrough from startup to shutdown.

---

## 1. Configuration — `config/settings.py`

Everything starts with `Settings`, a Pydantic class that reads config from your `.env` file (or environment variables directly). Nothing is hardcoded anywhere in the application code.

```
API_KEY, API_SECRET, KITE_USERNAME, KITE_PASSWORD, TOTP_SECRET  ← broker credentials
TRADE_MARGIN = 1,000,000  ← total capital budget
N_STRATEGIES = 3          ← how many strategies to pick today
ENTRY_TOLERANCE_SECONDS = 15  ← ±15s window around each entry time
MARKET_CLOSE_TIME = 15:30:00
```

---

## 2. Startup Sequence — `main.py`

When you run `python -m option_trading.main`, it executes 10 steps in order:

```
Step 1  Load Settings from .env
Step 2  Configure structured JSON logging (to console + app.log)
Step 3  autologin() → Selenium opens headless Chrome, logs into Kite, returns access_token
Step 4  Build KiteConnect instance, fetch all NFO instruments into a DataFrame (~50,000 rows)
Step 5  Read GROUPED_tier_1_results.csv, pick top N strategies for today's weekday by net profit
Step 6  DTE check — mark each strategy tradable=True only if both CE+PE expire TODAY
Step 7  Wire up AWS services: DynamoDB store, S3 archiver, SNS notifier
Step 8  Start HTTP health server on :8080 (background thread, for ECS health checks)
Step 9  Register SIGTERM handler → graceful position square-off before container exits
Step 10 Build StrategyEngine + WebSocketManager, call ws_manager.connect() → BLOCKS here
```

---

## 3. How Strategies Are Selected — `services/strategy_selector.py`

The CSV `GROUPED_tier_1_results.csv` is a historical performance table. For today:

1. Filter rows where `day == today's weekday` (e.g. `"tuesday"`)
2. Take the top N rows by `net profit`
3. For each row, call `get_tokens()` to resolve CE/PE symbols and instrument tokens using today's live spot price
4. Build a `Strategy` dataclass per row, keyed by `max_success_time` (e.g. `"11:24:40"`)
5. If two strategies share the same entry time, keep the one with higher net profit

---

## 4. Instrument Token Resolution — `services/instruments.py`

This is how the system finds which specific option contract to trade.

Given: `index = "NSE:NIFTY BANK"`, `strangle = 100`

```
1. Fetch live spot price via kite.ltp("NSE:NIFTY BANK") → e.g. 48,350
2. Round to nearest 100 (BANKNIFTY rule) → 48,400
3. CE strike = 48,400 + 100 = 48,500
4. PE strike = 48,400 - 100 = 48,300
5. Get expiry date → next Wednesday → format as "24621" (YYMONTHDD, no leading zero on month)
6. Build symbol: "BANKNIFTY2462148500CE" and "BANKNIFTY2462148300PE"
7. Look up in instruments_df by tradingsymbol → get instrument_token integers
```

**Two-tier fallback:**
- Try weekly expiry first (`expiry1`)
- If not found AND we're in the last 2 weeks of the month → try monthly expiry format (`24JUN` style)
- If still not found → return `(None, None, None, None)` → strategy gets marked skipped

The **date format** `format_date()` strips leading zeros only from the month: June 21, 2024 → `"24621"` (not `"240621"`).

### Strike Rounding Rules

| Index | Short Symbol | Expiry Day | Strike Rounding |
|---|---|---|---|
| `NSE:NIFTY 50` | `NIFTY` | Thursday | 50 |
| `NSE:NIFTY BANK` | `BANKNIFTY` | Wednesday | 100 |
| `NSE:NIFTY FIN SERVICE` | `FINNIFTY` | Tuesday | 50 |
| `NSE:NIFTY MID SELECT` | `MIDCPNIFTY` | Monday | 100 |

---

## 5. Real-Time Engine — `core/strategy_engine.py`

This is the heart. The WebSocket calls `engine.process_ticks(ticks)` on every incoming batch of market data. The engine does three things per call:

### A. Market Close Check (≥ 15:30)

If it's past market close time, it immediately squares off every running strategy and closes the WebSocket. Real strategies get actual BUY orders placed; dummy strategies just get logged.

### B. Strategy Lifecycle

Each strategy goes through states based on current time vs its entry time:

```
current_time > tolerance_after  AND  not running, not completed
    → SKIPPED (time elapsed without entry)

tolerance_before ≤ current_time ≤ tolerance_after  AND  tradable=False
    → AVOIDED (DTE check failed — not expiry day)

tolerance_before ≤ current_time ≤ tolerance_after  AND  tradable=True
    → START: running=True
    → Refresh tokens using live spot price (so strike matches market RIGHT NOW)
    → Update WebSocket subscription with new tokens
```

The token refresh at entry time is important — the strike computed at startup may no longer be at-the-money by the time the strategy executes.

### C. Execution (for each running strategy)

```
For each tick in the batch:
    → Update strategy.current_ce_price if tick matches ce_token
    → Update strategy.current_pe_price if tick matches pe_token
    → On first tick: check LIQUIDITY
        - Buy depth quantity > threshold AND sell depth quantity > threshold
        - Bid-ask spread < 1%
        - If no liquidity → mark skipped immediately

Once both CE+PE prices exist:

    If initial_sum is None (first time):
        → ENTRY: record initial_sum = ce_price + pe_price
        → Place SELL orders (real) or just log (dummy)
        → Handle partial fills: if only one leg went through → square off that leg immediately

    Else (subsequent ticks):
        → price_diff = current_sum - initial_sum
        → Compute stop_loss:
            if price_diff < 0 (in profit): stop_loss = points
            if price_diff ≥ 0 (in loss) and second==0: stop_loss = points
            if price_diff ≥ 0 (in loss) and second≠0: stop_loss = 1.0 × points
        → If abs(price_diff) >= stop_loss → EXIT
            → Place BUY orders (real) or log (dummy)
            → Set result = PROFIT if price_diff < 0, else LOSS
```

**Why selling = profit when price falls:** The strategy sells options (short strangle). You collect premium upfront. If the combined CE+PE price drops (`price_diff < 0`), that means options got cheaper — you profit when you buy them back cheaper than you sold.

**Conditional strategies:** A strategy with `conditional_on = "11:24:40"` will only enter if the strategy at `11:24:40` has `result == PROFIT`. This creates chained strategies where you only double-down when the first trade won.

### Liquidity Thresholds

| Index | Min Depth Quantity |
|---|---|
| `NSE:NIFTY FIN SERVICE` | 3,600 |
| `NSE:NIFTY BANK` | 3,000 |
| `NSE:NIFTY 50` | 1,000 |
| `NSE:NIFTY MID SELECT` | 0 (always liquid) |

---

## 6. Order Placement — `core/broker.py`

Every order goes through two layers of protection:

```
BrokerClient.place_order()
    ↓
CircuitBreaker.call()          ← OPEN? → return None immediately, no API call
    ↓
place_order() with retry       ← 3 attempts, 0.5s delay between each
    ↓
kite.place_order()             ← actual Kite API call
```

### Circuit Breaker States

- **CLOSED** (normal): calls go through; failures are counted
- **OPEN** (after 3 consecutive failures): all calls fast-fail immediately without hitting the API; stays open for 30 seconds
- **HALF_OPEN** (after 30s cooldown): one probe call is allowed through; if it succeeds → CLOSED; if it fails → OPEN again

This prevents the engine from hammering a broken API during network issues.

### Lot Sizes and Margins

| Index | Lot Size | Margin Required |
|---|---|---|
| `NSE:NIFTY BANK` | 15 | ₹2,20,000 |
| `NSE:NIFTY FIN SERVICE` | 40 | ₹2,80,000 |
| `NSE:NIFTY 50` | 25 | ₹2,20,000 |
| `NSE:NIFTY MID SELECT` | 50 | ₹2,00,000 |

`TRADE_MARGIN` tracks available capital in real time: decremented on entry orders, incremented on exit orders.

---

## 7. Logging — `utils/logging_setup.py`

Every log line is structured JSON:

```json
{
  "timestamp": "2026-06-21T10:30:15.123Z",
  "level": "INFO",
  "correlation_id": "abc12345",
  "logger": "option_trading.core.strategy_engine",
  "message": "Entry orders placed — ce_order=... pe_order=... margin=780000"
}
```

The `correlation_id` is set once per `process_ticks()` call using Python's `contextvars`. Every log line emitted during a single tick batch shares the same ID, so you can filter CloudWatch Logs Insights by that ID and see the complete trace for one strategy execution.

---

## 8. Persistence — `persistence/`

Three layers of storage:

**`TradeLogger`** — in-memory during the session; on shutdown saves to:
- `all_trade_data_YYYYMMDD.csv` (local file)
- DynamoDB `trading-engine-trades` table
- S3 bucket (via `S3Archiver`)

**`DynamoStateStore`** — saves strategy state (prices, order IDs, lifecycle status) to DynamoDB after each state transition. If the container crashes and restarts, it can read the last known state from DynamoDB instead of starting blind.

**`S3Archiver`** — uploads the daily CSV to `s3://option-trading-data/trade-data/all_trade_data_YYYYMMDD.csv` for permanent storage and later analysis.

---

## 9. Infrastructure — `deploy/aws/`

```
EventBridge → ECS RunTask  at 03:45 UTC (09:15 IST, market opens)
EventBridge → ECS StopTask at 10:15 UTC (15:45 IST, safety net)

ECS Fargate Task (single instance — NEVER two):
    - 1 vCPU, 2GB RAM
    - SIGTERM → engine squares off positions → flushes logs → exits
    - Restart policy: on-failure, max 3 retries
    - Health check: GET :8080/health every 30s

AWS Resources:
    DynamoDB: trading-engine-state  (strategy recovery on restart, 7-day TTL)
    DynamoDB: trading-engine-trades (trade audit trail, 7-day TTL)
    S3:       option-trading-data   (permanent CSV archive)
    SNS:      trading-alerts        (order failure and drawdown alerts)
    CloudWatch: /option-trading/engine (all structured JSON logs)
```

**Why single instance?** In trading, a duplicate order is worse than a missed order. Fault tolerance comes from auto-restart + DynamoDB state recovery, not from running multiple copies.

---

## 10. Local Development — `docker-compose.yml`

```bash
docker-compose up
```

This starts:
- The trading engine container (reads from `.env`)
- **LocalStack** — a local AWS emulator that fakes DynamoDB, S3, and SNS so you can develop without a real AWS account

---

## Full Data Flow

```
.env
 └─ Settings
     └─ autologin() → Selenium → access_token
         └─ KiteConnect → NFO instruments DataFrame
             └─ strategy_selector → top N strategies for today
                 └─ get_tokens() → CE/PE symbols + tokens for each
                     └─ StrategyEngine (initialized with all deps)
                         └─ WebSocketManager.connect()
                             └─ KiteTicker WebSocket
                                 └─ on_ticks → engine.process_ticks(ticks)
                                     ├─ _update_strategy_lifecycle()  (PENDING → RUNNING/SKIPPED)
                                     ├─ _execute_strategy()           (RUNNING: entry → exit)
                                     │   ├─ check_liquidity()
                                     │   ├─ broker.place_order() × 2  (SELL on entry)
                                     │   └─ broker.place_order() × 2  (BUY on exit)
                                     ├─ trade_logger.log()            (every event)
                                     └─ dynamo_store.save_state()     (after each transition)

                         on shutdown:
                             trade_logger.save_csv()
                             s3_archiver.archive()
                             trade_logger.save_to_dynamo()
```
