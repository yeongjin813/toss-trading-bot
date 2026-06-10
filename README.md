# Multi-Asset Live Signal Generator & Quantitative Trading Pipeline

An automated quantitative trading infrastructure and empirical data collection pipeline built on top of the Korea Investment & Securities (KIS) Overseas Market API. The system dynamically ingests live market sequences, computes multi-indicator analytics per asset, enforces liquidity/volatility risk controls, and emits standardized execution telemetry while gracefully bypassing mock-brokerage infrastructure constraints.

| Field | Value |
|---|---|
| **Current Phase** | **Phase 4** — Production-Grade Execution Engine & State-Gated Order Lifecycle |
| **Watchlist** | Configurable via `.env` (`WATCHLIST`); default `NVDA`, `PLTR`, `AAPL` |
| **Broker Infrastructure** | Korea Investment & Securities (KIS) Developers API (Virtual Trading Server) |
| **Target Unified Account** | `CANO` + `KIS_ACNT_PRDT_CD` from `.env` (no hardcoded credentials) |
| **Initial Capital Base** | `CAPITAL_AT_RISK` from `.env` (default `$10,000` per sizing model) |
| **Data Architecture** | Bootstrap full history once → 1-bar micro-fetch per 60-second cycle |

---

## Table of Contents

1. [Project Evolution & Development Milestones (Phase 1 – 4)](#1-project-evolution--development-milestones-phase-1--4)
2. [Phase 4 Production Architecture Overview](#2-phase-4-production-architecture-overview)
3. [Dynamic Memory Window Caching (`MarketDataCache`)](#3-dynamic-memory-window-caching-marketdatacache)
4. [State-Gated Synchronous Order Lifecycle Chain](#4-state-gated-synchronous-order-lifecycle-chain)
5. [Hybrid EOD and Intraday Scanning Engine](#5-hybrid-eod-and-intraday-scanning-engine)
6. [Liquidity Gate Calibration](#6-liquidity-gate-calibration)
7. [Configuration Externalization Matrix](#7-configuration-externalization-matrix)
8. [System Architecture & Core Subsystems](#8-system-architecture--core-subsystems)
9. [Infrastructure Patch Ledger (VTS Mock API Bypasses)](#9-infrastructure-patch-ledger-vts-mock-api-bypasses)
10. [Signal Priority & Execution Rules](#10-signal-priority--execution-rules)
11. [Optimized Strategy Parameters](#11-optimized-strategy-parameters)
12. [Operational Guidelines](#12-operational-guidelines)
13. [KIS API Reference (Live Production Paths)](#13-kis-api-reference-live-production-paths)
14. [Project Structure](#14-project-structure)
15. [Academic Regime Mapping (Watchlist Design)](#15-academic-regime-mapping-watchlist-design)
16. [License & Disclaimer](#license--disclaimer)

---

## 1. Project Evolution & Development Milestones (Phase 1 – 4)

### Phase 1: Static Backtesting Infrastructure (Baseline)

- **Architecture**: Single-ticker backtest on `AAPL` using a fixed 20-day SMA crossover over 3 years of historical daily data loaded from flat CSV files.
- **Performance**: Net return **+0.99%** — Final Portfolio Value **$10,099.42**.
- **Insight**: High vulnerability to whipsaws and capital erosion in sideways markets. A fixed lookback period without momentum or risk controls produced unreliable entry timing.

### Phase 2: Parameter Optimization & Multi-Indicator Filtering

- **Architecture**: Grid-search optimization (SMA 10–50, step 5) converged on a **10-day SMA** paired with a **14-day RSI** momentum filter (BUY when RSI >= 50; Overbought exit when RSI > 70 or SMA death cross).
- **Performance**: Net return **+0.52%** — Final Portfolio Value **$10,052.18**.
- **Insight**: Observed False Negative dilemma — stacking uncorrelated indicators choked profitable entries. Reduced whipsaw frequency came at the cost of missed momentum breakouts.

### Phase 3: Production-Ready Live Signal Pipeline

- **Architecture**: Replaced static CSV dependencies with automated, live API ingestion. Integrated credential encryption layers via `.env` state protection and established clean exception frameworks to process broker authorization sequences dynamically.
- **Capability**: Dynamic data persistence, standardized English console telemetry, and integrated Backtrader performance analyzers (Final Portfolio Value, Sharpe Ratio, Max Drawdown).

### Phase 4: Production-Grade Execution Engine *(Current)*

Phase 4 evolved from a fragile infinite polling loop into a **robust, production-ready quantitative execution engine**. The refactor preserves all existing infrastructure assets — KIS API wrappers, `.env` overrides, OAuth token caching, and KIS VTS exchange proxies for `PLTR` via `NASD` — while introducing five core architectural upgrades:

| Upgrade | Module | Summary |
|---|---|---|
| **Dynamic Memory Window Caching** | `main.py` → `MarketDataCache` | Full 756-bar bootstrap once; 1-bar micro-fetch per cycle |
| **State-Gated Order Lifecycle** | `main.py` + `analytics.py` | Deterministic dispatch → verify → lock → persist → transition |
| **Hybrid EOD / Intraday Engine** | `analytics.py` | Bar-trailing crossover gate + continuous ATR stop scanning |
| **Liquidity Gate Calibration** | `analytics.py` + `strategy.py` | Institutional volume surge filter at `Volume_SMA_20 × 1.2` |
| **Configuration Externalization** | `analytics.py` → `StrategyConfig.from_env()` | Zero hardcoded watchlist, capital, or strategy parameters |

Additional hardening delivered in Phase 4:

- **Exact liquidation quantity synchronization**: SELL orders clamp to `held_quantity` in persisted state — never sell more than locally tracked holdings.
- **Weekday market session gate**: Crossover signals suppressed on weekends via `is_us_equity_session()`.
- **Non-blocking balance inquiry**: `try_print_mock_account_balance()` remains optional; loop never deadlocks on VTS suffix mismatches.
- **Backtest parity**: `strategy.py` volume gate aligned to analytics (`volume > volume_sma × 1.2`).

---

## 2. Phase 4 Production Architecture Overview

The live engine operates as a **self-contained state machine** orchestrated by `main.py` and evaluated by `LiveSignalEngine` in `analytics.py`. Each 60-second cycle follows a deterministic pipeline:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         BOOTSTRAP (once at startup)                         │
│  validate_environment() → KIS OAuth token → MarketDataCache.bootstrap()     │
│  → load trading_state.json → optional balance probe → enter loop            │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    MONITORING LOOP (every LOOP_COOLDOWN_SECONDS)            │
│                                                                             │
│  For each ticker in WATCHLIST:                                              │
│    1. MarketDataCache.refresh_latest()     ← 1-bar micro-fetch only         │
│    2. LiveSignalEngine.evaluate_trading_cycle()  ← hybrid EOD + intraday      │
│    3. print_session_telemetry()             ← [METRICS] stream              │
│    4. dispatch_order_with_state_machine()   ← if actionable signal          │
│    5. save trading_state.json               ← deterministic persist         │
│                                                                             │
│  sleep(LOOP_COOLDOWN_SECONDS)                                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Runtime State Registry (`trading_state.json`)

Each watchlist ticker maintains an isolated `PositionState` record persisted to disk:

| Field | Type | Purpose |
|---|---|---|
| `in_position` | `bool` | Whether the engine considers the ticker held (`has_position` property) |
| `pending_order` | `bool` | Concurrency lock — suppresses duplicate signals while an order is in flight |
| `held_quantity` | `int` | Locally tracked share count for exact liquidation clamping |
| `last_processed_date` | `str \| null` | ISO date (`YYYY-MM-DD`) of the last crossover evaluation processed |
| `latest_bar_date` | `str \| null` | Active daily bar date for session-low tracking |
| `session_low` | `float \| null` | Running minimum low observed on the current daily bar (intraday ATR scan) |
| `highest_price_achieved` | `float \| null` | Peak close since entry (trailing stop anchor) |
| `current_atr` | `float \| null` | Latest Wilder ATR(14) at evaluation time |
| `dynamic_stop_distance` | `float \| null` | `ATR × atr_multiplier` |
| `trigger_floor` | `float \| null` | `highest_price_achieved − dynamic_stop_distance` |

Example persisted entry:

```json
{
  "NVDA": {
    "in_position": true,
    "pending_order": false,
    "last_processed_date": "2026-06-05",
    "latest_bar_date": "2026-06-05",
    "held_quantity": 12,
    "session_low": 138.42,
    "highest_price_achieved": 145.80,
    "current_atr": 4.215,
    "dynamic_stop_distance": 8.43,
    "trigger_floor": 137.37
  }
}
```

---

## 3. Dynamic Memory Window Caching (`MarketDataCache`)

### Problem Addressed

The pre-Phase-4 loop downloaded **~756 bars** (3 years × 252 trading days) of historical daily OHLCV for every watchlist asset on **every 60-second cycle**. This saturated KIS rate limits, triggered throttling, and wasted bandwidth on data that had not changed.

### Solution: Localized Memory Bar Pooling

`MarketDataCache` in `main.py` implements a **two-phase data ingestion strategy**:

#### Phase A — Bootstrap (Exactly Once at Startup)

```
MarketDataCache.bootstrap(WATCHLIST)
  │
  ├─ For each ticker:
  │    ├─ Attempt load from data/{ticker}_daily.csv (if ≥ 22 bars)
  │    └─ Else: KISApiClient.fetch_us_daily_bars() → paginated full history
  │         TR ID: HHDFS76240000 | target: TARGET_BARS = 756
  │
  ├─ Store in self._frames[ticker]  (in-memory pandas DataFrame)
  └─ Persist snapshot to data/{ticker}_daily.csv
```

At startup, the orchestrator prints:

```
Bootstrapping historical market data cache (one-time full fetch)...
  -> Bootstrap NVDA: 756 bars cached to ./data/nvda_daily.csv
  -> Bootstrap PLTR: 756 bars cached to ./data/pltr_daily.csv
  -> Bootstrap AAPL: 756 bars cached to ./data/aapl_daily.csv
```

#### Phase B — Incremental Micro-Fetch (Every Loop Iteration)

```
MarketDataCache.refresh_latest(ticker)
  │
  ├─ KISApiClient.fetch_us_daily_latest()  ← single API call, one page
  │
  ├─ If latest_date NOT in cached frame:
  │    └─ Append new row → deduplicate → sort → tail(756) → is_new_bar=True
  │
  └─ If latest_date already exists (same trading day):
       └─ In-place OHLCV update on existing row → is_new_bar=False
```

This design reduces per-cycle network traffic from **O(756 × N tickers)** API rows to **O(1 × N tickers)** — a single latest-bar page per asset per cycle.

### Memory Window Invariants

| Constant | Value | Role |
|---|---|---|
| `LOOKBACK_YEARS` | 3 | Historical depth target |
| `TARGET_BARS` | 756 | Rolling window cap (`3 × 252`) |
| `MIN_DATA_BARS` | 22 | Minimum bars before analytics may run |
| `DATA_DIR` | `./data` | On-disk CSV mirror of in-memory frames |

The in-memory DataFrame and on-disk CSV stay synchronized: after each `refresh_latest()`, `persist_market_data()` writes the updated frame back to `data/{ticker}_daily.csv`.

### Network Traffic Comparison

| Mode | API Calls per Cycle (3 tickers) | Bars Transferred (approx.) |
|---|---|---|
| **Pre-Phase-4** (full re-fetch) | 3 × ~15 paginated requests | ~2,268 bars |
| **Phase-4** (`MarketDataCache`) | 3 × 1 request | ~3 bars (latest page only) |

---

## 4. State-Gated Synchronous Order Lifecycle Chain

### Problem Addressed

The legacy loop evaluated an EOD strategy every 60 seconds against the same daily bar, generating **duplicate BUY/SELL signals** and spamming KIS with redundant orders. Additionally, KIS `rt_cd == "0"` confirms successful **transmission**, not guaranteed **execution** — state was previously saved before verifying broker receipt, creating desynchronization on API failures.

### Solution: Deterministic Sequential Pipeline

All order execution flows through `dispatch_order_with_state_machine()` in `main.py`. The lifecycle enforces strict ordering with **no state mutation before broker verification**:

```
┌──────────────┐    ┌─────────────────┐    ┌──────────────────────┐
│ Pre-Flight   │    │  KIS Dispatch   │    │  Response Verify     │
│ Gate Check   │───▶│  execute_broker │───▶│  rt_cd == "0"        │
│ pending_order│    │  _order()       │    │  (raise on failure)  │
└──────────────┘    └─────────────────┘    └──────────┬───────────┘
                                                      │
                                                      ▼
┌──────────────┐    ┌─────────────────┐    ┌──────────────────────┐
│ Final Persist│◀───│ Execution       │◀───│ Interim Persist      │
│ pending=False│    │ Confirmation    │    │ pending_order = True │
│ + transition │    │ apply_post_     │    │ save trading_state   │
│ save to disk │    │ order_transition│    │ .json (crash protect)│
└──────────────┘    └─────────────────┘    └──────────────────────┘
```

### Step-by-Step Lifecycle

| Step | Action | Module | State Mutation |
|---|---|---|---|
| **0** | If `runtime.pending_order == True` → suppress signal, return `LOCKED` | `main.py` | None |
| **1** | Resolve execution quantity (`_resolve_execution_quantity`) | `main.py` | None |
| **2** | **KIS Dispatch** — `execute_broker_order()` → `place_us_order()` with hashkey | `main.py` | None |
| **3** | **Verification** — assert `rt_cd == "0"`; `place_us_order()` raises on rejection | `main.py` | None |
| **4** | **Lock** — set `pending_order = True` | `main.py` | In-memory |
| **5** | **Interim Save** — `save_persisted_states()` to disk | `main.py` | Crash protection |
| **6** | **Execution Confirmation** — `apply_post_order_transition()` updates position, clears lock | `analytics.py` | Full transition |
| **7** | **Final Persist** — `save_persisted_states()` with confirmed state | `main.py` | Committed |

### Concurrency Lock Semantics

When `pending_order` is `True`:

- `evaluate_trading_cycle()` returns `signal: "HOLD"` with `pending_order_locked: True`.
- `dispatch_order_with_state_machine()` immediately returns `LOCKED` without contacting KIS.
- The lock prevents duplicate order spam across consecutive 60-second cycles.

If the process crashes between Step 5 (interim save) and Step 6 (transition), `pending_order` remains `True` on restart, blocking further orders until manual intervention or state correction — a deliberate trade-off favoring **no duplicate transmissions** over **automatic retry**.

### Post-Order State Transitions (`apply_post_order_transition`)

| Signal | `in_position` | `held_quantity` | Trailing Fields | `last_processed_date` |
|---|---|---|---|---|
| `BUY` | `True` | `filled_quantity` | Peak initialized | Set to `current_bar_date` |
| `SELL` | `False` | `0` | Cleared | Set to `current_bar_date` |
| `DYNAMIC_ATR_SELL` | `False` | `0` | Cleared | Set to `current_bar_date` |

All transitions set `pending_order = False` upon completion.

### Exact Liquidation Quantity Synchronization

Before any `SELL` or `DYNAMIC_ATR_SELL`, `_resolve_execution_quantity()` enforces:

```
execution_quantity = min(proposed_size, held_quantity)
```

If `held_quantity <= 0`, the liquidation is skipped entirely — preventing broker rejections from order volume exceeding held balance. This replaces the legacy pattern of issuing market sells using static calculated targets (`quantity = size`) that could mismatch real holdings.

---

## 5. Hybrid EOD and Intraday Scanning Engine

### Problem Addressed

Running an EOD (end-of-day) crossover strategy inside a 60-second polling loop caused **identical daily bars to be evaluated repeatedly**, re-firing golden-cross and death-cross signals on every cycle. The system also attempted crossover evaluation on **weekends** when US equity markets are closed.

### Solution: Dual-Mode Evaluation Architecture

`LiveSignalEngine.evaluate_trading_cycle()` in `analytics.py` implements a **hybrid scanning model**:

- **EOD crossover baseline** — BUY/SELL signals fire at most **once per daily bar**.
- **Intraday risk engine** — Priority-1 `DYNAMIC_ATR_SELL` scans continuously every 60 seconds using the running session low.

```
                    evaluate_trading_cycle()
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
    is_us_equity_session()          update_session_low()
    (weekday gate)                  (track min low on active bar)
              │                               │
              └───────────────┬───────────────┘
                              ▼
              should_allow_crossover_signals()
                              │
              ┌───────────────┴───────────────┐
              │                               │
     allow_crossover = True          allow_crossover = False
     (new bar + weekday)             (same bar OR weekend)
              │                               │
              ▼                               ▼
    Full evaluate_bar():            evaluate_bar() with
    ATR stop + BUY + SELL           crossover suppressed:
                                    ATR stop ONLY
```

### Bar-Trailing Gate: `should_allow_crossover_signals()`

```python
def should_allow_crossover_signals(
    current_bar_date: str,
    last_processed_date: str | None,
    market_open: bool,
) -> bool:
    if not market_open:
        return False
    if last_processed_date is None:
        return True
    return current_bar_date != last_processed_date
```

| Condition | Crossover BUY/SELL | DYNAMIC_ATR_SELL |
|---|---|---|
| New daily bar + weekday | **Allowed** | **Active** |
| Same daily bar (`current_bar_date == last_processed_date`) | **Suppressed** | **Active** |
| Weekend (`market_open == False`) | **Suppressed** | **Active** (if in position) |
| `pending_order == True` | **Suppressed** (locked) | **Suppressed** (locked) |

When crossover is suppressed, `evaluate_bar()` returns `HOLD` with `crossover_suppressed: True` — but the **Priority-1 ATR stop path executes first**, before the crossover gate is checked.

### Intraday Session Low Tracking

On each cycle, `update_session_low()` maintains the minimum low observed for the active daily bar:

```
If latest_bar_date != current_bar_date:
    session_low = bar.low          ← new trading day reset
Else:
    session_low = min(session_low, bar.low)   ← intraday accumulation
```

The ATR liquidation trigger uses this **running session low** rather than only the static EOD low stored in the historical dataframe row:

```
Liquidation Trigger:  session_low ≤ trigger_floor
Where:
    trigger_floor = highest_price_achieved − (ATR(14) × 2.0)
```

This guarantees the system acts as a **true intraday risk engine** over an EOD signal baseline — protecting open positions between daily bar closes without re-firing duplicate entry/exit crossover signals.

### Crossover Deduplication on HOLD

When no order is placed and crossover evaluation is allowed, `mark_crossover_processed()` sets `last_processed_date = current_bar_date` for `BUY`, `SELL`, and `HOLD` signals — ensuring the same bar's crossover logic is not re-evaluated on subsequent 60-second cycles.

---

## 6. Liquidity Gate Calibration

### Institutional Volume Surge Filter

Long entry (`BUY`) signals require confirmation of an **institutional volume surge** — participation materially above the 20-day average — before capital is deployed. This mitigates slippage and false breakouts on thin-volume sessions, particularly on high-momentum names such as `PLTR`.

### Precise Filtering Rule

Implemented identically in `analytics.py` (`LiveSignalEngine._passes_volume_filter`) and `strategy.py` (`SmaCross._passes_volume_filter`):

```
Volume Gate PASS (entry permitted):
    Volume_t > Volume_SMA_20 × volume_threshold

Volume Gate FAIL (signal downgraded to HOLD):
    Volume_t ≤ Volume_SMA_20 × volume_threshold

Default: volume_threshold = 1.2  (120% of 20-day average)
```

Where:

- `Volume_t` — latest session volume from the active daily bar.
- `Volume_SMA_20` — simple moving average of volume over `volume_sma_period = 20` bars.
- `volume_threshold` — configurable via `.env` (`VOLUME_THRESHOLD`) or `StrategyConfig.from_env()`.

### Signal Interaction

| Signal | Volume Gate Required? | Behavior on FAIL |
|---|---|---|
| `BUY` | **Yes** | Downgraded to `HOLD` with `liquidity_blocked: True` |
| `SELL` | No | Death cross / RSI exit proceeds regardless |
| `DYNAMIC_ATR_SELL` | No | Risk liquidation proceeds regardless |
| `HOLD` | N/A | Default state |

The `[METRICS]` telemetry stream reports the live volume ratio for empirical validation:

```
[METRICS] PLTR | 2026-06-05 14:30:01 | Close=142.50 | SMA20=138.20 | RSI=58.32 |
ATR_Wilder=4.2150 | Volume=45,230,000/38,100,000 (1.187x)
```

A ratio below `1.200x` indicates the liquidity gate would block a concurrent BUY signal.

---

## 7. Configuration Externalization Matrix

### Zero-Leak Environment Architecture

Phase 4 eliminates hardcoded watchlist entries, capital baselines, and strategy parameters from orchestrator scripts. All runtime configuration flows through:

1. **`.env`** — local secrets and deployment overrides (never committed).
2. **`.env.example`** — documented template with placeholder values (safe to commit).
3. **`StrategyConfig.from_env()`** — typed dataclass loader with sensible defaults.

`.gitignore` enforces zero credential leakage by excluding:

```
.env
kis_token_cache.json
trading_state.json
project_metrics.log
__pycache__/
```

`load_dotenv(override=True)` in `main.py` ensures `.env` values take precedence over stale shell exports when `CANO`, suffix, or watchlist changes between sessions.

### Environment Variable Reference

#### KIS Broker Credentials

| Variable | Required | Default | Description |
|---|---|---|---|
| `KIS_APP_KEY` | **Yes** | — | App Key from KIS Developers (VTS / mock trading) |
| `KIS_APP_SECRET` | **Yes** | — | App Secret from KIS Developers |
| `KIS_CANO` | **Yes** | — | 8-digit account number prefix |
| `KIS_ACNT_PRDT_CD` | **Yes** | — | 2-digit product suffix (e.g. `01`, `02`) |

#### Portfolio & Watchlist

| Variable | Required | Default | Description |
|---|---|---|---|
| `WATCHLIST` | No | `NVDA,PLTR,AAPL` | Comma-separated ticker symbols (uppercased at load) |
| `CAPITAL_AT_RISK` | No | `10000` | USD capital base for position sizing |
| `RISK_PER_TRADE` | No | `0.01` | Fraction of capital risked per trade (1%) |

#### Loop Timing

| Variable | Required | Default | Description |
|---|---|---|---|
| `LOOP_COOLDOWN_SECONDS` | No | `60` | Inter-cycle pause in the monitoring loop |
| `TICKER_SLEEP_SECONDS` | No | `1` | Pause between tickers within a single cycle |

#### Strategy Parameters (via `StrategyConfig.from_env()`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `SMA_PERIOD` | No | `10` | Price trend filter (days) |
| `RSI_PERIOD` | No | `14` | RSI lookback (Wilder smoothing) |
| `ATR_PERIOD` | No | `14` | ATR lookback (Wilder smoothing) |
| `VOLUME_SMA_PERIOD` | No | `20` | Volume moving average period |
| `ATR_MULTIPLIER` | No | `2.0` | Trailing stop buffer multiplier |
| `RSI_BUY_THRESHOLD` | No | `50` | Minimum RSI for BUY confirmation |
| `RSI_UPPER_LIMIT` | No | `70` | RSI overbought exit threshold |
| `VOLUME_THRESHOLD` | No | `1.2` | Volume surge multiplier (liquidity gate) |
| `RSI_EXIT_MODE` | No | `crossdown` | `crossdown` or `threshold` |
| `USE_TRAILING_STOP` | No | `true` | Enable dynamic ATR trailing stop |
| `EXECUTION_MODE` | No | `eod` | Signal execution timing mode |

### Loading Chain

```
.env  ──▶  load_dotenv(override=True)
              │
              ├─▶  WATCHLIST          → _parse_watchlist() in main.py
              ├─▶  CAPITAL_AT_RISK    → position sizing in process_ticker()
              ├─▶  RISK_PER_TRADE     → calculate_position_size() in analytics.py
              └─▶  StrategyConfig.from_env()  → STRATEGY_CONFIG in main.py
                                                    └─▶ LiveSignalEngine(STRATEGY_CONFIG)
                                                         └─▶ strategy.py load_backtest_config()
```

### Example `.env` Configuration

Copy from `.env.example` and substitute your credentials:

```ini
# KIS Virtual Trading Server credentials
KIS_APP_KEY=your_app_key_here
KIS_APP_SECRET=your_app_secret_here
KIS_CANO=50191906
KIS_ACNT_PRDT_CD=02

# Portfolio configuration
WATCHLIST=NVDA,PLTR,AAPL
CAPITAL_AT_RISK=10000
RISK_PER_TRADE=0.01

# Loop timing
LOOP_COOLDOWN_SECONDS=60
TICKER_SLEEP_SECONDS=1

# Optional strategy overrides
SMA_PERIOD=10
RSI_PERIOD=14
ATR_PERIOD=14
ATR_MULTIPLIER=2.0
VOLUME_THRESHOLD=1.2
RSI_EXIT_MODE=crossdown
```

**Never commit `.env` to version control.**

---

## 8. System Architecture & Core Subsystems

### Multi-Asset Tracking Matrix

Each watchlist ticker is processed through an independent analytics pipeline, sharing parameter rules but maintaining isolated `PositionState` records in `trading_state.json`.

```
                     ┌─────────────────────────────────────────────┐
                     │             main.py — Orchestrator          │
                     │   WATCHLIST from .env | MarketDataCache     │
                     │   dispatch_order_with_state_machine()       │
                     └─────────────────────┬───────────────────────┘
                                           │
          ┌────────────────────────────────┼────────────────────────────────┐
          │                                │                                │
          ▼                                ▼                                ▼
 ┌─────────────────┐              ┌─────────────────┐              ┌─────────────────┐
 │  NVDA Pipeline  │              │  PLTR Pipeline  │              │  AAPL Pipeline  │
 ├─────────────────┤              ├─────────────────┤              ├─────────────────┤
 │ 1-bar refresh   │              │ 1-bar refresh   │              │ 1-bar refresh   │
 │ evaluate_trading│              │ evaluate_trading│              │ evaluate_trading│
 │ _cycle()        │              │ _cycle()        │              │ _cycle()        │
 │ State machine   │              │ (NASD proxy)    │              │ State machine   │
 │ order dispatch  │              │ order dispatch  │              │ order dispatch  │
 └─────────────────┘              └─────────────────┘              └─────────────────┘
          │                                │                                │
          └────────────────────────────────┼────────────────────────────────┘
                                           ▼
                     ┌─────────────────────────────────────────────┐
                     │   [METRICS] Stream / trading_state.json      │
                     └─────────────────────────────────────────────┘
```

| Layer | Module | Responsibility |
|---|---|---|
| Orchestration | `main.py` | KIS auth, `MarketDataCache`, 60s watchlist loop, state-gated order routing, `[METRICS]` telemetry |
| Indicators & State Machine | `analytics.py` | Wilder RSI/ATR, SMA, Volume SMA; `LiveSignalEngine.evaluate_trading_cycle()`; `PositionState` lifecycle |
| Backtest Strategy | `strategy.py` | Backtrader `SmaCross` with env-driven params, RSI filter, volume gate, dynamic ATR trailing stop |
| Persistence | `data/{ticker}_daily.csv`, `trading_state.json` | OHLCV cache mirror and per-ticker runtime registry |

### Strategic Parameter Telemetry Disconnect

To capture cross-regime statistical divergence for empirical visualization, the pipeline implements an intentional telemetry disconnect:

- **Execution Signal Engine**: Evaluates entries using a short-term trend index ($\text{SMA}_{10}$) via `StrategyConfig` for higher breakout sensitivity.
- **Telemetry Logging Layer**: Independent extraction of a medium-term structural baseline ($\text{SMA}_{20}$) mapped onto the streaming telemetry log for external regression/confusion-matrix modeling (`ANALYTICS_SMA_PERIOD = 20` in `main.py`).

### Position Sizing Formula

Share count is computed by `calculate_position_size()` in `analytics.py`:

```
Shares = floor((CAPITAL_AT_RISK × RISK_PER_TRADE) / stop_distance)

Where:
    stop_distance = ATR(14) × atr_multiplier
```

Both `CAPITAL_AT_RISK` and `RISK_PER_TRADE` are loaded from `.env` — not hardcoded in the orchestrator.

---

## 9. Infrastructure Patch Ledger (VTS Mock API Bypasses)

During live integration deployment, several severe sandbox anomalies within the KIS Virtual Trading Server (VTS) were programmatically intercepted and neutralized. **All patches remain active in Phase 4.**

### Incident 1: Core API Query Failure (`403 Forbidden` / Suffix Mismatch)

- **Symptom**: Connection configurations using legacy real-money environments or mismatched product suffixes threw persistent structural verification faults (`INPUT INVALID_CHECK_ACNO`, `OPSQ2000`).
- **Resolution**: Hardened the authentication wrapper to dynamically prioritize environment overrides (`load_dotenv(override=True)`). Balance inquiry is wrapped in `try_print_mock_account_balance()` — failures are logged as `[INFO]` and never block the monitoring loop. Order placement proceeds under `CAPITAL_AT_RISK` from `.env`, not live balance.

### Incident 2: High-Momentum Target Data Truncation (`PLTR` Candle Drop)

- **Symptom**: Querying daily historical bars for NYSE-listed tickers (`PLTR`) with standard exchange mappings (`excd: "NYS"`) returned an empty bar array (0 bars cached) due to VTS database replication lag.
- **Resolution**: Patched the routing logic inside the `MARKET_META` configuration block. Forced the execution engine to proxy `PLTR` candle and order execution sequences via the NASDAQ gateway (`excd: "NAS"`, `ovrs_excg_cd: "NASD"`). This effectively bypassed the mock data drop, successfully caching **756** historical bars for quantitative metrics processing.

### Incident 3: Invalid Balance TR ID (`VTTT3402R`)

- **Symptom**: User-specified TR ID `VTTT3402R` returned `OPSQ0002` (invalid TR code) on the VTS server.
- **Resolution**: Confirmed working mock TR for present-balance inquiry is **`VTRP6504R`** (`/uapi/overseas-stock/v1/trading/inquire-present-balance`). Inquiry remains optional and non-blocking.

### KIS Exchange Routing Table (`MARKET_META`)

| Ticker | Regime Role | `excd` (Price) | `ovrs_excg_cd` (Orders) |
|---|---|---|---|
| `NVDA` | High-volatility market leader | `NAS` | `NASD` |
| `PLTR` | High-momentum breakout (VTS proxy) | `NAS` | `NASD` |
| `AAPL` | Low-volatility large-cap baseline | `NAS` | `NASD` |

New watchlist tickers must be registered in `MARKET_META` inside `main.py` with valid KIS exchange codes before being added to `WATCHLIST` in `.env`.

---

## 10. Signal Priority & Execution Rules

Signal evaluation runs independently per ticker according to strict descending criteria priorities:

| Priority | Signal Code | Terminal Banner / Log Ingestion Template | Trigger Condition |
|---|---|---|---|
| **1** | `DYNAMIC_ATR_SELL` | `[KIS ORDER] SELL {TICKER} \| close-position \| market order` | Position active AND $\text{session\_low} \le \text{Peak} - (\text{ATR}_{14} \times 2.0)$ |
| **2** | `SELL` | `[KIS ORDER] SELL {TICKER} \| close-position \| market order` | Death Cross ($\text{Close}_t < \text{SMA}_{10}$ while $\text{Close}_{t-1} \ge \text{SMA}_{10}$) OR RSI crossdown exit ($\text{RSI}_{t-1} \ge 70$ and $\text{RSI}_t < 70$) — *only on new daily bar* |
| **3** | `BUY` | `[KIS ORDER] BUY {TICKER} \| market order` | Golden Cross ($\text{Close}_t > \text{SMA}_{10}$ while $\text{Close}_{t-1} \le \text{SMA}_{10}$) AND $\text{RSI}_{14} \ge 50$ AND Volume Gate **PASS** — *only on new daily bar* |
| **4** | `HOLD` | `[METRICS] ... Signal: HOLD` | Default state; BUY invalidated if Volume Gate **FAIL** or crossover suppressed |

### Dynamic ATR Stop Formula

```
Dynamic Stop Distance = ATR(14) × 2.0
Trigger Floor Price   = Highest Close Since Entry − Dynamic Stop Distance
Liquidation Trigger   = session_low ≤ Trigger Floor    (evaluated every 60 seconds)
```

### Crossover Suppression Rules

Crossover signals (Priority 2 and 3) are evaluated **only when** `should_allow_crossover_signals()` returns `True`. Priority 1 (`DYNAMIC_ATR_SELL`) is **never suppressed** by the bar-trailing gate (unless `pending_order` is locked).

---

## 11. Optimized Strategy Parameters

All parameters below are defaults. Override any value via `.env` (see [Configuration Externalization Matrix](#7-configuration-externalization-matrix)).

| Parameter | Default | Env Override | Description |
|---|---|---|---|
| `sma_period` | **10** | `SMA_PERIOD` | Price trend filter (days) — execution signal engine |
| `ANALYTICS_SMA_PERIOD` | **20** | — | Structural baseline for `[METRICS]` telemetry only (hardcoded in `main.py`) |
| `rsi_period` | **14** | `RSI_PERIOD` | Momentum oscillator lookback (days); Wilder smoothing |
| `rsi_buy_threshold` | **50** | `RSI_BUY_THRESHOLD` | Minimum RSI to confirm a BUY signal |
| `rsi_upper_limit` | **70** | `RSI_UPPER_LIMIT` | RSI overbought exit threshold (crossdown mode) |
| `rsi_exit_mode` | **crossdown** | `RSI_EXIT_MODE` | Exit when RSI crosses below 70 from above |
| `atr_period` | **14** | `ATR_PERIOD` | Average True Range lookback (days); Wilder smoothing |
| `atr_multiplier` | **2.0** | `ATR_MULTIPLIER` | Volatility buffer multiplier for trailing stop |
| `volume_sma_period` | **20** | `VOLUME_SMA_PERIOD` | Volume moving average for liquidity gate |
| `volume_threshold` | **1.2** | `VOLUME_THRESHOLD` | Volume must exceed 120% of 20-day average for BUY |
| `use_trailing_stop` | **true** | `USE_TRAILING_STOP` | Enable dynamic ATR trailing stop |
| `commission_rate` | **0.001** | — | Backtest transaction cost (0.1% per trade) |
| `min_data_bars` | **22** | — | Minimum bar count required before analytics execution |
| `CAPITAL_AT_RISK` | **10000** | `CAPITAL_AT_RISK` | Manual capital base for live position sizing (USD) |
| `RISK_PER_TRADE` | **0.01** | `RISK_PER_TRADE` | Fraction of capital risked per trade (1%) |
| `TARGET_BARS` | **756** | — | Rolling historical window (3 years × 252 days) |
| `LOOP_COOLDOWN_SECONDS` | **60** | `LOOP_COOLDOWN_SECONDS` | Inter-cycle pause |
| `TICKER_SLEEP_SECONDS` | **1** | `TICKER_SLEEP_SECONDS` | Pause between tickers within a cycle |

---

## 12. Operational Guidelines

### Dependency Installation

```powershell
pip install -r requirements.txt
```

```
backtrader
matplotlib
numpy
pandas
python-dotenv
requests
yfinance
```

### Launch Continuous Monitor

```powershell
python main.py
```

Expected startup sequence:

1. Environment validation (`validate_environment()`) — KIS credentials + `MARKET_META` routing for all `WATCHLIST` tickers
2. KIS OAuth token issuance (cached in `kis_token_cache.json`)
3. **One-time bootstrap** — `MarketDataCache.bootstrap()` fetches ~756 bars per ticker
4. Load `trading_state.json` (or initialize empty registry)
5. Optional balance probe (`try_print_mock_account_balance`) — failures ignored
6. Capital model banner (`log_configured_capital_model`) — confirms `CAPITAL_AT_RISK` from `.env`
7. Infinite watchlist loop with per-ticker `[METRICS]` emission and state-gated order dispatch

Expected runtime log markers:

```
Order pipeline: KIS dispatch -> rt_cd verify -> state transition -> persist
[GATE] US equity session closed (weekend) — crossover signals suppressed
[LOCK] NVDA pending_order=True — signal suppressed
[SKIP] PLTR liquidation skipped — no held quantity tracked
  -> 756 bars in memory (intraday refresh)
  -> Order accepted (rt_cd=0). State transition applied for NVDA: has_position=True, held_qty=12
```

### Empirical Log Redirection

Redirect the full telemetry stream for offline visualization (confusion matrices, drawdown curves, regime comparison):

```powershell
python main.py *> project_metrics.log
```

Sample `[METRICS]` line (pipe-delimited for parsing):

```
[METRICS] NVDA | 2026-06-05 14:30:01 | Close=142.50 | SMA20=138.20 | RSI=58.32 | ATR_Wilder=4.2150 | Volume=45,230,000/38,100,000 (1.187x)
```

Extended telemetry fields emitted per cycle:

```
Bar Date             : 2026-06-05
Session Low          : 138.4200
Market Open Gate     : True
Crossover Allowed    : False
Signal               : HOLD
Runtime Registry     : pending=False | held_qty=12 | last_processed=2026-06-05
ATR Stop Telemetry   : peak=$145.80 | floor=$137.37 | session_low=$138.42
```

### Verification Suite

```powershell
python test_analytics.py
```

Runs unit tests against `LiveSignalEngine`:

- Wilder RSI/ATR/SMA indicator computation
- O(1) state replay determinism
- Bar-by-bar BUY → peak tracking → DYNAMIC_ATR_SELL transitions
- External state serialization round-trip (all `PositionState` fields including `pending_order`, `held_quantity`, `session_low`)

### Graceful Shutdown

Press `Ctrl+C` during the monitoring loop. The handler persists the latest multi-ticker state to `trading_state.json` before exit.

---

## 13. KIS API Reference (Live Production Paths)

| Operation | TR ID (Mock) | HTTP Path | Phase 4 Usage |
|---|---|---|---|
| OAuth Token | — | `POST /oauth2/tokenP` | Cached in `kis_token_cache.json` |
| US Daily OHLCV (full) | `HHDFS76240000` | `GET /uapi/overseas-price/v1/quotations/dailyprice` | Bootstrap only (~756 bars) |
| US Daily OHLCV (latest) | `HHDFS76240000` | `GET /uapi/overseas-price/v1/quotations/dailyprice` | Every cycle (1 page) |
| US Market Buy | `VTTT1002U` | `POST /uapi/overseas-stock/v1/trading/order` | State-gated dispatch |
| US Market Sell | `VTTT1006U` | `POST /uapi/overseas-stock/v1/trading/order` | Clamped to `held_quantity` |
| Present Balance *(optional)* | `VTRP6504R` | `GET /uapi/overseas-stock/v1/trading/inquire-present-balance` | Non-blocking probe |
| Order Hashkey | — | `POST /uapi/hashkey` | Required before every order |

**Base URL (VTS Mock):** `https://openapivts.koreainvestment.com:29443`

---

## 14. Project Structure

```
Toss Trading Bot/
├── main.py                 # KIS orchestrator, MarketDataCache, state-gated order machine, [METRICS]
├── analytics.py            # LiveSignalEngine, PositionState, StrategyConfig.from_env(), cycle evaluation
├── strategy.py             # Backtrader SmaCross backtest strategy (env-driven params)
├── test_analytics.py       # Engine verification suite
├── requirements.txt        # Python dependencies
├── .env.example            # Full configuration template (copy to .env)
├── .gitignore              # Excludes .env, kis_token_cache.json, trading_state.json
├── trading_state.json      # Multi-ticker PositionState registry (auto-generated)
├── kis_token_cache.json    # OAuth token cache (auto-generated)
├── project_metrics.log     # Example redirected telemetry output
└── data/
    ├── nvda_daily.csv      # OHLCV cache (bootstrap + incremental refresh)
    ├── pltr_daily.csv
    └── aapl_daily.csv
```

---

## 15. Academic Regime Mapping (Watchlist Design)

The default watchlist (`NVDA`, `PLTR`, `AAPL`) targets three distinct volatility regimes. Customize via `WATCHLIST` in `.env` while registering exchange routing in `MARKET_META`.

| Ticker | Volatility Regime | Primary Experimental Target |
|---|---|---|
| `NVDA` | High-volatility market leader | Dynamic ATR Trailing Stop stress test (intraday session_low scan) |
| `PLTR` | High-momentum growth / breakout | Volume Liquidity Gate validation + NASD VTS proxy |
| `AAPL` | Low-volatility large-cap control | Baseline comparative analysis + crossover deduplication |

This three-asset design enables cross-regime empirical comparison: ATR stop behavior under elevated realized volatility (`NVDA`), liquidity-filter efficacy on momentum names (`PLTR`), and signal stability on a mature benchmark (`AAPL`).

---

## License & Disclaimer

This repository is an academic quantitative infrastructure project. It connects to the **KIS Virtual Trading Server** for sandbox execution only. Past backtest performance (Phase 1–2 figures) does not guarantee future results. Always validate signals, `.env` configuration, and account routing independently before any real-capital deployment.
