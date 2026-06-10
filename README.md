# Multi-Asset Live Signal Generator & Quantitative Trading Pipeline

An automated quantitative trading infrastructure and empirical data collection pipeline built on top of the Korea Investment & Securities (KIS) Overseas Market API. The system dynamically ingests live market sequences, computes multi-indicator analytics per asset, enforces liquidity/volatility risk controls, and emits standardized execution telemetry while gracefully bypassing mock-brokerage infrastructure constraints.

| Field | Value |
|---|---|
| **Current Phase** | **Phase 4** — Production-Grade Execution Engine, Mathematical Logic Corrections & State-Gated Order Lifecycle |
| **Watchlist** | Configurable via `.env` (`WATCHLIST`); default `NVDA`, `PLTR`, `AAPL` |
| **Broker Infrastructure** | Korea Investment & Securities (KIS) Developers API (Virtual Trading Server) |
| **Target Unified Account** | `CANO` + `KIS_ACNT_PRDT_CD` from `.env` (no hardcoded credentials) |
| **Initial Capital Base** | `CAPITAL_AT_RISK` from `.env` (default `$10,000` per sizing model) |
| **Data Architecture** | Bootstrap full history once → 1-bar micro-fetch per 60-second cycle |
| **Session Gate** | NYSE holiday registry + weekday filter (`is_us_equity_session()`) |
| **Execution Integrity** | Same-bar sequential ATR stop → trailing update → crossover/RSI exit |

---

## Table of Contents

1. [Project Evolution & Development Milestones (Phase 1 – 4)](#1-project-evolution--development-milestones-phase-1--4)
2. [Phase 4 Production Architecture Overview](#2-phase-4-production-architecture-overview)
3. [Look-Ahead Bias Eradication (Same-Bar Sequential Integrity)](#3-look-ahead-bias-eradication-same-bar-sequential-integrity)
4. [Refactored RSI Momentum Exit (Crossdown Mode)](#4-refactored-rsi-momentum-exit-crossdown-mode)
5. [Dual-Clamp Capital-Resilient Position Sizing](#5-dual-clamp-capital-resilient-position-sizing)
6. [NYSE Holiday Registry Infrastructure](#6-nyse-holiday-registry-infrastructure)
7. [Dynamic Memory Window Caching (`MarketDataCache`)](#7-dynamic-memory-window-caching-marketdatacache)
8. [State-Gated Synchronous Order Lifecycle Chain](#8-state-gated-synchronous-order-lifecycle-chain)
9. [Hybrid EOD and Intraday Scanning Engine](#9-hybrid-eod-and-intraday-scanning-engine)
10. [Liquidity Gate Calibration](#10-liquidity-gate-calibration)
11. [Configuration Externalization Matrix](#11-configuration-externalization-matrix)
12. [System Architecture & Data Topology](#12-system-architecture--data-topology)
13. [Infrastructure Patch Ledger (VTS Mock API Bypasses)](#13-infrastructure-patch-ledger-vts-mock-api-bypasses)
14. [Signal Priority & Execution Rules](#14-signal-priority--execution-rules)
15. [Optimized Strategy Parameters](#15-optimized-strategy-parameters)
16. [Operational Guidelines](#16-operational-guidelines)
17. [KIS API Reference (Live Production Paths)](#17-kis-api-reference-live-production-paths)
18. [Project Structure](#18-project-structure)
19. [Academic Regime Mapping (Watchlist Design)](#19-academic-regime-mapping-watchlist-design)
20. [License & Disclaimer](#license--disclaimer)

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

Phase 4 evolved from a fragile infinite polling loop into a **robust, production-ready quantitative execution engine** with rigorous **mathematical logic corrections**. The refactor preserves all existing infrastructure assets — KIS API wrappers, `.env` overrides, OAuth token caching, and KIS VTS exchange proxies for `PLTR` via `NASD` — while delivering the following architectural and quantitative upgrades:

| Upgrade | Module | Summary |
|---|---|---|
| **Same-Bar Sequential Integrity** | `analytics.py` + `strategy.py` | ATR stop evaluated against *prior* floor before peak update — eliminates look-ahead bias |
| **RSI Crossdown Exit** | `analytics.py` + `strategy.py` | Exit only on verified crossdown event, not blind >70 threshold |
| **Dual-Clamp Position Sizing** | `analytics.py` | Risk budget ∩ capital budget prevents over-leverage rejections |
| **NYSE Holiday Registry** | `analytics.py` + `main.py` | Full US market calendar replaces naive `weekday < 5` gate |
| **Dynamic Memory Window Caching** | `main.py` → `MarketDataCache` | Full 756-bar bootstrap once; 1-bar micro-fetch per cycle |
| **State-Gated Order Lifecycle** | `main.py` + `analytics.py` | Deterministic dispatch → verify → lock → persist → transition |
| **Hybrid EOD / Intraday Engine** | `analytics.py` | Bar-trailing crossover gate + continuous ATR stop scanning |
| **Liquidity Gate Calibration** | `analytics.py` + `strategy.py` | Institutional volume surge filter at `Volume_SMA_20 × 1.2` |
| **Configuration Externalization** | `analytics.py` → `StrategyConfig.from_env()` | Zero hardcoded watchlist, capital, or strategy parameters |

Additional hardening delivered in Phase 4:

- **Exact liquidation quantity synchronization**: SELL orders clamp to `held_quantity` in persisted state — never sell more than locally tracked holdings.
- **Non-blocking balance inquiry**: `try_print_mock_account_balance()` remains optional; loop never deadlocks on VTS suffix mismatches.
- **Backtest/live parity**: `strategy.py` and `analytics.py` share identical trailing-stop sequence, RSI crossdown logic, and volume gate rules.

---

## 2. Phase 4 Production Architecture Overview

The live engine operates as a **self-contained state machine** orchestrated by `main.py` and evaluated by `LiveSignalEngine` in `analytics.py`. Each monitoring cycle follows a deterministic pipeline:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         BOOTSTRAP (once at startup)                         │
│  validate_environment() → KIS OAuth token → MarketDataCache.bootstrap()     │
│  → load trading_state.json → optional balance probe → enter loop            │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│              MONITORING LOOP (every LOOP_COOLDOWN_SECONDS = 60)             │
│                                                                             │
│  IF describe_us_market_closure() != None:                                   │
│      → skip entire cycle, sleep MARKET_CLOSED_SLEEP_SECONDS (3600)          │
│  ELSE:                                                                      │
│      For each ticker in WATCHLIST:                                          │
│        1. MarketDataCache.refresh_latest()     ← 1-bar micro-fetch only     │
│        2. LiveSignalEngine.evaluate_trading_cycle()                         │
│        3. print_session_telemetry()             ← [METRICS] stream          │
│        4. dispatch_order_with_state_machine()   ← if actionable signal    │
│        5. save trading_state.json               ← deterministic persist     │
│      sleep(LOOP_COOLDOWN_SECONDS)                                           │
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
| `trigger_floor` | `float \| null` | **Prior-bar** trailing floor: `highest_price_achieved − dynamic_stop_distance` |

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

The **`trigger_floor`** field is the critical persisted artifact for look-ahead-free stop evaluation: it reflects the floor computed at the *end of the previous bar*, never inflated by the current bar's close before the intraday low check runs.

---

## 3. Look-Ahead Bias Eradication (Same-Bar Sequential Integrity)

### Problem Statement

A common quantitative pitfall occurs when a trailing stop engine **updates the highest peak and recalculates the trigger floor using the current bar's close** *before* testing whether the current bar's low breached the prior floor. This introduces **same-bar look-ahead bias**: the stop floor is artificially raised by information (the close) that would not have been known at the moment the intraday low occurred.

This bias affects both:
- **Historical backtests** (`strategy.py` / Backtrader `SmaCross.next()`)
- **Live evaluation cycles** (`analytics.py` / `LiveSignalEngine.evaluate_bar()`)

### Corrected 3-Step Position-Active Sequence

When a position is active, both modules now execute this **strict sequential integrity chain**:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  POSITION ACTIVE — Same-Bar Sequential Integrity (No Look-Ahead)          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  STEP 1 — Prior-Floor ATR Stop Check (IMMEDIATE)                        │
│  ───────────────────────────────────────────────                        │
│  Condition:  Low_t ≤ Prior Trigger Floor                                │
│              (uses persisted trigger_floor / self.trigger_floor)         │
│              NOT recalculated with current bar close                     │
│                                                                         │
│  If TRUE  → DYNAMIC_ATR_SELL / self.close() → RETURN (exit bar logic)   │
│  If FALSE → proceed to Step 2                                           │
│                                                                         │
│  STEP 2 — Trailing State Update (SURVIVAL CONFIRMED ONLY)               │
│  ─────────────────────────────────────────────────────────              │
│  Update highest_price_achieved with current close (if higher)           │
│  Recalculate dynamic_stop_distance and trigger_floor with current ATR   │
│                                                                         │
│  STEP 3 — Alternative Standard Exits                                    │
│  ────────────────────────────────────                                   │
│  Evaluate Death Cross (Close_t < SMA_10 cross)                          │
│  Evaluate RSI Crossdown exit (see Section 4)                            │
│  If triggered → SELL / self.close()                                     │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Mathematical Stop Condition (Step 1)

$$
\text{Liquidation Trigger} \iff \text{Low}_t \le \text{Prior Trigger Floor}
$$

Where:

$$
\text{Prior Trigger Floor} = \text{Peak}_{t-1} - (\text{ATR}_{t-1} \times \text{Multiplier})
$$

The prior floor is read from persisted state (`PositionState.trigger_floor` in live mode, `self.trigger_floor` in backtest mode) **without** first applying $\text{Close}_t$ to the peak.

### Implementation Map

| Step | `analytics.py` (`evaluate_bar`) | `strategy.py` (`SmaCross.next`) |
|---|---|---|
| **1** | `_dynamic_atr_stop_triggered(state, effective_low)` using prior `trigger_floor` | `_dynamic_atr_stop_triggered(bar_low)` using prior `self.trigger_floor` |
| **2** | `_update_trailing_state(state, bar.close, bar.atr)` | `_update_trailing_state(close)` → recomputes `self.trigger_floor` |
| **3** | `_death_cross()` OR `_rsi_triggers_exit(rsi, prev_rsi)` | `crossover < 0` OR `_rsi_triggers_exit()` |

### Live Intraday Extension

During live 60-second polling cycles, Step 1 uses the **running session low** (`session_low`) rather than only the static EOD low from the dataframe row:

$$
\text{session\_low} = \min(\text{Low observed on active daily bar})
$$

$$
\text{Liquidation Trigger (live)} \iff \text{session\_low} \le \text{Prior Trigger Floor}
$$

This preserves intraday risk protection without re-firing crossover signals on the same daily bar.

### Verification

`test_analytics.py` Test 3 validates the corrected sequence:

1. **BUY** at close 101.0 — peak and floor initialized.
2. **HOLD** at close 110.0 — floor updated to 106.0 only after surviving Step 1.
3. **DYNAMIC_ATR_SELL** when bar low (105.50) ≤ prior floor (106.00) — stop fires **before** any close-based peak inflation on that bar.

---

## 4. Refactored RSI Momentum Exit (Crossdown Mode)

### Problem Statement

The RSI overbought exit must **not** trigger blindly upon entering the >70 territory. A position could remain valid while RSI sustains above 70 during a strong trend. Exiting merely because $\text{RSI}_t > 70$ would cause premature liquidation and diverge from the Phase 2 optimization intent.

### Crossdown Exit Specification (Default Mode)

The system executes RSI exits only on a verified **Crossdown event**:

$$
\text{RSI Exit Trigger} \iff \text{RSI}_{t-1} \ge 70 \quad \text{AND} \quad \text{RSI}_t < 70
$$

| Mode | Env Variable | Condition | Use Case |
|---|---|---|---|
| **crossdown** *(default)* | `RSI_EXIT_MODE=crossdown` | $\text{RSI}_{t-1} \ge \text{upper\_limit}$ AND $\text{RSI}_t < \text{upper\_limit}$ | Confirmed momentum reversal from overbought |
| **threshold** | `RSI_EXIT_MODE=threshold` | $\text{RSI}_t > \text{upper\_limit}$ | Aggressive overbought exit (non-default) |

Default `upper_limit = 70` (override via `RSI_UPPER_LIMIT` in `.env`).

### Implementation Parity

Both execution paths implement identical crossdown logic:

**`analytics.py` — `LiveSignalEngine._rsi_triggers_exit()`**

```python
def _rsi_triggers_exit(self, rsi: float, prev_rsi: float) -> bool:
    upper = self.config.rsi_upper_limit
    if self.config.rsi_exit_mode == "threshold":
        return rsi > upper
    return prev_rsi >= upper and rsi < upper
```

**`strategy.py` — `SmaCross._rsi_triggers_exit()`**

```python
def _rsi_triggers_exit(self) -> bool:
    upper = self.params.rsi_sell_threshold
    if self.params.rsi_exit_mode == "threshold":
        return float(self.rsi[0]) > upper
    return float(self.rsi[-1]) >= upper and float(self.rsi[0]) < upper
```

### Execution Priority Relative to ATR Stop

RSI crossdown evaluation occurs exclusively in **Step 3** — after the position survives the prior-floor ATR stop check (Step 1) and trailing state update (Step 2). This ordering guarantees that volatility liquidation takes precedence over momentum reversal signals.

---

## 5. Dual-Clamp Capital-Resilient Position Sizing

### Problem Statement

Pure risk-based sizing ($\lfloor \text{Dollar Risk} / \text{Stop Distance} \rfloor$) can propose share counts whose **notional cost exceeds deployable capital**, especially on high-priced momentum names such as `PLTR`. KIS mock and live brokers reject these orders with insufficient-funds errors, particularly when micro-volatility scaling produces oversized risk allocations.

### Dual-Constraint Framework

`calculate_position_size()` in `analytics.py` computes a **dual-clamp** that intersects the risk budget with the capital budget:

$$
\text{shares\_by\_risk} = \left\lfloor \frac{\text{Dollar Risk}}{\text{Stop Distance}} \right\rfloor
$$

$$
\text{shares\_by\_capital} = \left\lfloor \frac{\text{Capital At Risk}}{\text{Current Price}} \right\rfloor
$$

$$
\text{Final Shares} = \max\left(1,\; \min(\text{shares\_by\_risk},\; \text{shares\_by\_capital})\right)
$$

Where:

| Variable | Source | Description |
|---|---|---|
| **Dollar Risk** | `CAPITAL_AT_RISK × RISK_PER_TRADE` | Maximum capital willing to lose per trade |
| **Stop Distance** | `ATR(14) × atr_multiplier` | Dynamic trailing stop width at entry |
| **Capital At Risk** | `.env` `CAPITAL_AT_RISK` | Deployable capital ceiling for notional sizing |
| **Current Price** | Latest bar close | Entry price proxy for notional calculation |

### Guard Conditions

The function returns **`0`** (no order) when:

- `entry_price <= 0`, `stop_distance <= 0`, or `risk_per_trade <= 0`
- `deployable capital <= 0`
- Either `shares_by_risk <= 0` or `shares_by_capital <= 0`

Otherwise it returns at least **1 share** when both constraints permit trading.

### Implementation

```python
def calculate_position_size(
    capital_at_risk: float,
    risk_per_trade: float,
    entry_price: float,
    stop_distance: float,
    available_capital: float | None = None,
) -> int:
    deployable = available_capital if available_capital is not None else capital_at_risk
    dollar_risk = capital_at_risk * risk_per_trade
    shares_by_risk = int(dollar_risk / stop_distance)
    shares_by_capital = int(deployable / entry_price)
    if shares_by_risk <= 0 or shares_by_capital <= 0:
        return 0
    return max(1, min(shares_by_risk, shares_by_capital))
```

Called from `LiveSignalEngine.evaluate_trading_cycle()` with `available_capital=capital_at_risk`.

### Worked Example

| Parameter | Value |
|---|---|
| `CAPITAL_AT_RISK` | $10,000 |
| `RISK_PER_TRADE` | 1% → Dollar Risk = $100 |
| `ATR(14)` | $4.00 → Stop Distance = $8.00 (×2.0) |
| `Current Price` | $180.00 (e.g., PLTR) |

$$
\text{shares\_by\_risk} = \lfloor 100 / 8.0 \rfloor = 12
$$

$$
\text{shares\_by\_capital} = \lfloor 10{,}000 / 180.0 \rfloor = 55
$$

$$
\text{Final Shares} = \max(1,\; \min(12,\; 55)) = 12
$$

If price were $900 with the same risk parameters:

$$
\text{shares\_by\_capital} = \lfloor 10{,}000 / 900.0 \rfloor = 11
$$

$$
\text{Final Shares} = \max(1,\; \min(12,\; 11)) = 11 \quad \text{(capital clamp binds)}
$$

---

## 6. NYSE Holiday Registry Infrastructure

### Problem Statement

The legacy session gate (`weekday < 5`) treated **US market holidays as normal trading days**, causing the monitoring loop to:

- Query stale broker environments on full market closures
- Waste API quota on days with no meaningful price discovery
- Risk network-layer instability from repeated failed or empty responses

### Replacement Architecture

The simple weekday check has been replaced with a **full-scale automated US Market Holiday Calendar framework** in `analytics.py`, consumed by `main.py` for loop gating.

#### Core Functions

| Function | Module | Purpose |
|---|---|---|
| `us_market_holidays_for_year(year)` | `analytics.py` | Returns `{date: holiday_name}` registry for a calendar year |
| `is_us_market_holiday(day)` | `analytics.py` | Boolean holiday check for a given date |
| `describe_us_market_closure(now)` | `analytics.py` | Returns `"weekend"`, holiday name, or `None` (open) |
| `is_us_equity_session(now)` | `analytics.py` | `True` only on non-weekend, non-holiday dates |

#### Registered NYSE Full-Day Closures

| Holiday | Calculation Rule | Observed-Date Handling |
|---|---|---|
| **New Year's Day** | January 1 | Friday if Saturday; Monday if Sunday |
| **Memorial Day** | Last Monday in May | Floating |
| **Independence Day** | July 4 | Friday if Saturday; Monday if Sunday |
| **Labor Day** | First Monday in September | Floating |
| **Thanksgiving** | Fourth Thursday in November | Floating |
| **Christmas** | December 25 | Friday if Saturday; Monday if Sunday |

#### Holiday Registry Implementation

```python
def us_market_holidays_for_year(year: int) -> dict[date, str]:
    return {
        _observed_fixed_holiday(year, 1, 1): "New Year's Day",
        _memorial_day(year): "Memorial Day",
        _observed_fixed_holiday(year, 7, 4): "Independence Day",
        _labor_day(year): "Labor Day",
        _thanksgiving_day(year): "Thanksgiving",
        _observed_fixed_holiday(year, 12, 25): "Christmas",
    }
```

### Extended Sleep Cycle on Market Closure

When `describe_us_market_closure()` returns a non-`None` value (weekend or registered holiday), `main.py` **skips the entire watchlist cycle** and enters an extended sleep:

| Variable | Default | Description |
|---|---|---|
| `MARKET_CLOSED_SLEEP_SECONDS` | **3600** (1 hour) | Sleep duration when market is closed |

```
--- Cycle N skipped at 2026-12-25 09:00:00 ---
[GATE] US market closed (Christmas). Sleeping 3600 seconds...
```

During open sessions, the standard `LOOP_COOLDOWN_SECONDS = 60` inter-cycle pause applies.

### Crossover Signal Interaction

`is_us_equity_session()` feeds into `should_allow_crossover_signals()`, suppressing BUY/SELL crossover evaluation on weekends and holidays. Priority-1 `DYNAMIC_ATR_SELL` intraday scanning remains architecturally available during partial sessions when the loop runs; on full closure days the cycle skip prevents all broker queries entirely.

---

## 7. Dynamic Memory Window Caching (`MarketDataCache`)

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

## 8. State-Gated Synchronous Order Lifecycle Chain

### Problem Addressed

The legacy loop evaluated an EOD strategy every 60 seconds against the same daily bar, generating **duplicate BUY/SELL signals** and spamming KIS with redundant orders. Additionally, KIS `rt_cd == "0"` confirms successful **transmission**, not guaranteed **execution** — state was previously saved before verifying broker receipt, creating desynchronization on API failures.

### Solution: Deterministic Sequential Pipeline

All order execution flows through `dispatch_order_with_state_machine()` in `main.py`:

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

### Exact Liquidation Quantity Synchronization

Before any `SELL` or `DYNAMIC_ATR_SELL`:

```
execution_quantity = min(proposed_size, held_quantity)
```

If `held_quantity <= 0`, liquidation is skipped — preventing broker rejections from order volume exceeding held balance.

---

## 9. Hybrid EOD and Intraday Scanning Engine

### Dual-Mode Evaluation Architecture

`LiveSignalEngine.evaluate_trading_cycle()` implements:

- **EOD crossover baseline** — BUY/SELL signals fire at most **once per daily bar**.
- **Intraday risk engine** — Priority-1 `DYNAMIC_ATR_SELL` uses running `session_low` every 60 seconds.

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
| New daily bar + open session | **Allowed** | **Active** |
| Same daily bar | **Suppressed** | **Active** (session_low scan) |
| Weekend or NYSE holiday | **Suppressed** | Cycle skipped entirely |
| `pending_order == True` | **Suppressed** (locked) | **Suppressed** (locked) |

When crossover is suppressed, Step 1 (prior-floor ATR stop) still executes before the crossover gate — preserving intraday risk protection on open-session same-bar cycles.

---

## 10. Liquidity Gate Calibration

### Institutional Volume Surge Filter

Long entry (`BUY`) signals require an **institutional volume surge**:

```
Volume Gate PASS (entry permitted):
    Volume_t > Volume_SMA_20 × volume_threshold

Volume Gate FAIL (signal downgraded to HOLD):
    Volume_t ≤ Volume_SMA_20 × volume_threshold

Default: volume_threshold = 1.2  (120% of 20-day average)
```

Implemented identically in `analytics.py` (`LiveSignalEngine._passes_volume_filter`) and `strategy.py` (`SmaCross._passes_volume_filter`).

| Signal | Volume Gate Required? | Behavior on FAIL |
|---|---|---|
| `BUY` | **Yes** | Downgraded to `HOLD` with `liquidity_blocked: True` |
| `SELL` | No | Death cross / RSI exit proceeds regardless |
| `DYNAMIC_ATR_SELL` | No | Risk liquidation proceeds regardless |
| `HOLD` | N/A | Default state |

Sample `[METRICS]` volume ratio line:

```
[METRICS] PLTR | 2026-06-05 14:30:01 | Close=142.50 | SMA20=138.20 | RSI=58.32 |
ATR_Wilder=4.2150 | Volume=45,230,000/38,100,000 (1.187x)
```

A ratio below `1.200x` indicates the liquidity gate would block a concurrent BUY signal.

---

## 11. Configuration Externalization Matrix

### Zero-Leak Environment Architecture

All runtime configuration flows through:

1. **`.env`** — local secrets and deployment overrides (never committed).
2. **`.env.example`** — documented template with placeholder values (safe to commit).
3. **`StrategyConfig.from_env()`** — typed dataclass loader with sensible defaults.

`.gitignore` enforces zero credential leakage:

```
.env
kis_token_cache.json
trading_state.json
project_metrics.log
__pycache__/
```

`load_dotenv(override=True)` ensures `.env` values take precedence over stale shell exports.

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
| `WATCHLIST` | No | `NVDA,PLTR,AAPL` | Comma-separated ticker symbols |
| `CAPITAL_AT_RISK` | No | `10000` | USD capital base for position sizing |
| `RISK_PER_TRADE` | No | `0.01` | Fraction of capital risked per trade (1%) |

#### Loop Timing

| Variable | Required | Default | Description |
|---|---|---|---|
| `LOOP_COOLDOWN_SECONDS` | No | `60` | Inter-cycle pause during open sessions |
| `TICKER_SLEEP_SECONDS` | No | `1` | Pause between tickers within a cycle |
| `MARKET_CLOSED_SLEEP_SECONDS` | No | `3600` | Extended sleep on weekends/holidays |

#### Strategy Parameters (via `StrategyConfig.from_env()`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `SMA_PERIOD` | No | `10` | Price trend filter (days) |
| `RSI_PERIOD` | No | `14` | RSI lookback (Wilder smoothing) |
| `ATR_PERIOD` | No | `14` | ATR lookback (Wilder smoothing) |
| `VOLUME_SMA_PERIOD` | No | `20` | Volume moving average period |
| `ATR_MULTIPLIER` | No | `2.0` | Trailing stop buffer multiplier |
| `RSI_BUY_THRESHOLD` | No | `50` | Minimum RSI for BUY confirmation |
| `RSI_UPPER_LIMIT` | No | `70` | RSI overbought crossdown threshold |
| `VOLUME_THRESHOLD` | No | `1.2` | Volume surge multiplier (liquidity gate) |
| `RSI_EXIT_MODE` | No | `crossdown` | `crossdown` or `threshold` |
| `USE_TRAILING_STOP` | No | `true` | Enable dynamic ATR trailing stop |
| `EXECUTION_MODE` | No | `eod` | Signal execution timing mode |

### Example `.env` Configuration

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
MARKET_CLOSED_SLEEP_SECONDS=3600

# Optional strategy overrides
SMA_PERIOD=10
RSI_PERIOD=14
ATR_PERIOD=14
ATR_MULTIPLIER=2.0
VOLUME_THRESHOLD=1.2
RSI_EXIT_MODE=crossdown
RSI_UPPER_LIMIT=70
```

**Never commit `.env` to version control.**

---

## 12. System Architecture & Data Topology

### Multi-Asset Tracking Matrix

Each watchlist ticker is processed through an independent analytics pipeline, sharing parameter rules but maintaining isolated `PositionState` records in `trading_state.json`.

```
                     ┌─────────────────────────────────────────────┐
                     │             main.py — Orchestrator          │
                     │   WATCHLIST from .env | MarketDataCache     │
                     │   NYSE holiday gate | state-gated orders    │
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
 │ 3-step bar eval │              │ (NASD proxy)    │              │ 3-step bar eval │
 │ dual-clamp size │              │ dual-clamp size │              │ dual-clamp size │
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
| Orchestration | `main.py` | KIS auth, `MarketDataCache`, holiday gate, 60s watchlist loop, state-gated order routing, `[METRICS]` telemetry |
| Indicators & State Machine | `analytics.py` | Wilder RSI/ATR, SMA, dual-clamp sizing, NYSE calendar, 3-step bar evaluation, `PositionState` lifecycle |
| Backtest Strategy | `strategy.py` | Backtrader `SmaCross` with identical 3-step trailing sequence and RSI crossdown parity |
| Persistence | `data/{ticker}_daily.csv`, `trading_state.json` | OHLCV cache mirror and per-ticker runtime registry |

### Strategic Parameter Telemetry Disconnect

- **Execution Signal Engine**: Evaluates entries using $\text{SMA}_{10}$ via `StrategyConfig` for higher breakout sensitivity.
- **Telemetry Logging Layer**: Independent extraction of $\text{SMA}_{20}$ (`ANALYTICS_SMA_PERIOD = 20` in `main.py`) for external regression and confusion-matrix modeling.

### Continuous Monitoring Loop (Detailed)

```
validate_environment()
  → KIS token (cached: kis_token_cache.json)
  → MarketDataCache.bootstrap()          # 756 bars × tickers, once
  → load trading_state.json
  → try_print_mock_account_balance()     # non-blocking
  → log_configured_capital_model()
  → while True:
        IF market closed (weekend/holiday):
            sleep(MARKET_CLOSED_SLEEP_SECONDS)
            continue
        run_watchlist_cycle()
        sleep(LOOP_COOLDOWN_SECONDS)
```

---

## 13. Infrastructure Patch Ledger (VTS Mock API Bypasses)

During live integration deployment, several severe sandbox anomalies within the KIS Virtual Trading Server (VTS) were programmatically intercepted and neutralized. **All patches remain active in Phase 4.**

### Incident 1: Core API Query Failure (`403 Forbidden` / Suffix Mismatch)

- **Symptom**: Connection configurations using legacy real-money environments or mismatched product suffixes threw persistent structural verification faults (`INPUT INVALID_CHECK_ACNO`, `OPSQ2000`).
- **Resolution**: Hardened authentication wrapper with `load_dotenv(override=True)`. Balance inquiry wrapped in `try_print_mock_account_balance()` — failures logged as `[INFO]` and never block the monitoring loop.

### Incident 2: High-Momentum Target Data Truncation (`PLTR` Candle Drop)

- **Symptom**: Querying daily historical bars for NYSE-listed tickers (`PLTR`) with standard exchange mappings (`excd: "NYS"`) returned an empty bar array (0 bars) due to VTS database replication lag.
- **Resolution**: Patched routing in `MARKET_META`. Forced `PLTR` candle and order execution through the NASDAQ gateway.

### Incident 3: Invalid Balance TR ID (`VTTT3402R`)

- **Symptom**: TR ID `VTTT3402R` returned `OPSQ0002` (invalid TR code) on VTS.
- **Resolution**: Working mock TR is **`VTRP6504R`** on `/uapi/overseas-stock/v1/trading/inquire-present-balance`. Inquiry remains optional and non-blocking.

### KIS Exchange Routing Table (`MARKET_META`)

| Ticker | Regime Role | `excd` (Price API) | `ovrs_excg_cd` (Order API) | Notes |
|---|---|---|---|---|
| `NVDA` | High-volatility market leader | `NAS` | `NASD` | Standard NASDAQ routing |
| `PLTR` | High-momentum breakout | `NAS` | `NASD` | **VTS proxy** — bypasses 0-bar NYS drop |
| `AAPL` | Low-volatility large-cap baseline | `NAS` | `NASD` | Standard NASDAQ routing |

New watchlist tickers must be registered in `MARKET_META` inside `main.py` before being added to `WATCHLIST` in `.env`.

---

## 14. Signal Priority & Execution Rules

Signal evaluation runs independently per ticker according to strict descending criteria priorities. **Within an active position, Steps 1–3 of Section 3 execute before any signal priority table lookup.**

| Priority | Signal Code | Terminal Banner | Trigger Condition |
|---|---|---|---|
| **1** | `DYNAMIC_ATR_SELL` | `[KIS ORDER] SELL {TICKER} \| close-position` | Step 1: $\text{Low}_t \le \text{Prior Trigger Floor}$ (or `session_low` live) |
| **2** | `SELL` | `[KIS ORDER] SELL {TICKER} \| close-position` | Step 3: Death Cross OR RSI crossdown ($\text{RSI}_{t-1} \ge 70$ AND $\text{RSI}_t < 70$) — *new daily bar only* |
| **3** | `BUY` | `[KIS ORDER] BUY {TICKER} \| market order` | Golden Cross AND $\text{RSI}_{14} \ge 50$ AND Volume Gate PASS — *new daily bar only* |
| **4** | `HOLD` | `[METRICS] ... Signal: HOLD` | Default; BUY blocked on liquidity fail or crossover suppression |

### Dynamic ATR Stop Formula

```
Dynamic Stop Distance = ATR(14) × 2.0
Prior Trigger Floor   = Peak_{t-1} − Dynamic Stop Distance_{t-1}
Liquidation (Step 1)  = Low_t ≤ Prior Trigger Floor
Post-Survival Update  = Peak_t = max(Peak_{t-1}, Close_t); recalculate floor
```

### RSI Exit Formula (Crossdown Mode — Default)

$$
\text{RSI Exit} \iff \text{RSI}_{t-1} \ge 70 \quad \text{AND} \quad \text{RSI}_t < 70
$$

RSI sustained above 70 without crossing down does **not** trigger an exit.

---

## 15. Optimized Strategy Parameters

All parameters below are defaults. Override via `.env` (see [Section 11](#11-configuration-externalization-matrix)).

| Parameter | Default | Env Override | Description |
|---|---|---|---|
| `sma_period` | **10** | `SMA_PERIOD` | Price trend filter — execution signal engine |
| `ANALYTICS_SMA_PERIOD` | **20** | — | Structural baseline for `[METRICS]` telemetry only |
| `rsi_period` | **14** | `RSI_PERIOD` | RSI lookback; Wilder smoothing |
| `rsi_buy_threshold` | **50** | `RSI_BUY_THRESHOLD` | Minimum RSI to confirm BUY |
| `rsi_upper_limit` | **70** | `RSI_UPPER_LIMIT` | Crossdown overbought threshold |
| `rsi_exit_mode` | **crossdown** | `RSI_EXIT_MODE` | Verified crossdown (not blind >70) |
| `atr_period` | **14** | `ATR_PERIOD` | ATR lookback; Wilder smoothing |
| `atr_multiplier` | **2.0** | `ATR_MULTIPLIER` | Trailing stop buffer multiplier |
| `volume_sma_period` | **20** | `VOLUME_SMA_PERIOD` | Volume moving average for liquidity gate |
| `volume_threshold` | **1.2** | `VOLUME_THRESHOLD` | Volume must exceed 120% of 20-day average |
| `use_trailing_stop` | **true** | `USE_TRAILING_STOP` | Enable dynamic ATR trailing stop |
| `commission_rate` | **0.001** | — | Backtest transaction cost (0.1%) |
| `min_data_bars` | **22** | — | Minimum bars before analytics execution |
| `CAPITAL_AT_RISK` | **10000** | `CAPITAL_AT_RISK` | Deployable capital for dual-clamp sizing |
| `RISK_PER_TRADE` | **0.01** | `RISK_PER_TRADE` | Per-trade risk fraction (1%) |
| `TARGET_BARS` | **756** | — | Rolling historical window (3 × 252) |
| `LOOP_COOLDOWN_SECONDS` | **60** | `LOOP_COOLDOWN_SECONDS` | Open-session inter-cycle pause |
| `MARKET_CLOSED_SLEEP_SECONDS` | **3600** | `MARKET_CLOSED_SLEEP_SECONDS` | Closed-session sleep duration |
| `TICKER_SLEEP_SECONDS` | **1** | `TICKER_SLEEP_SECONDS` | Pause between tickers within a cycle |

---

## 16. Operational Guidelines

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

1. Environment validation — KIS credentials + `MARKET_META` routing for all `WATCHLIST` tickers
2. KIS OAuth token issuance (cached in `kis_token_cache.json`)
3. **One-time bootstrap** — `MarketDataCache.bootstrap()` fetches ~756 bars per ticker
4. Load `trading_state.json` (or initialize empty registry)
5. Optional balance probe — failures ignored (`[INFO]`)
6. Capital model banner — confirms `CAPITAL_AT_RISK` from `.env`
7. Infinite watchlist loop with `[METRICS]` emission and state-gated order dispatch

Expected runtime log markers:

```
Order pipeline: KIS dispatch -> rt_cd verify -> state transition -> persist
--- Cycle 12 skipped at 2026-12-25 09:00:00 ---
[GATE] US market closed (Christmas). Sleeping 3600 seconds...
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
Position Size (shares): 12
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
- **3-step bar transitions**: BUY → peak tracking → prior-floor DYNAMIC_ATR_SELL (look-ahead free)
- External state serialization round-trip (all `PositionState` fields)

### Graceful Shutdown

Press `Ctrl+C` during the monitoring loop. The handler persists the latest multi-ticker state to `trading_state.json` before exit.

---

## 17. KIS API Reference (Live Production Paths)

| Operation | TR ID (Mock) | HTTP Path | Phase 4 Usage |
|---|---|---|---|
| OAuth Token | — | `POST /oauth2/tokenP` | Cached in `kis_token_cache.json` |
| US Daily OHLCV (full) | `HHDFS76240000` | `GET /uapi/overseas-price/v1/quotations/dailyprice` | Bootstrap only (~756 bars) |
| US Daily OHLCV (latest) | `HHDFS76240000` | `GET /uapi/overseas-price/v1/quotations/dailyprice` | Every open-session cycle (1 page) |
| US Market Buy | `VTTT1002U` | `POST /uapi/overseas-stock/v1/trading/order` | State-gated dispatch |
| US Market Sell | `VTTT1006U` | `POST /uapi/overseas-stock/v1/trading/order` | Clamped to `held_quantity` |
| Present Balance *(optional)* | `VTRP6504R` | `GET /uapi/overseas-stock/v1/trading/inquire-present-balance` | Non-blocking probe |
| Order Hashkey | — | `POST /uapi/hashkey` | Required before every order |

**Base URL (VTS Mock):** `https://openapivts.koreainvestment.com:29443`

---

## 18. Project Structure

```
Toss Trading Bot/
├── main.py                 # KIS orchestrator, MarketDataCache, holiday gate, state-gated orders, [METRICS]
├── analytics.py            # LiveSignalEngine, PositionState, dual-clamp sizing, NYSE calendar, 3-step bar eval
├── strategy.py             # Backtrader SmaCross — look-ahead-free trailing stop + RSI crossdown parity
├── test_analytics.py       # Engine verification suite (includes 3-step transition tests)
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

## 19. Academic Regime Mapping (Watchlist Design)

The default watchlist (`NVDA`, `PLTR`, `AAPL`) targets three distinct volatility regimes. Customize via `WATCHLIST` in `.env` while registering exchange routing in `MARKET_META`.

| Ticker | Volatility Regime | Primary Experimental Target |
|---|---|---|
| `NVDA` | High-volatility market leader | 3-step ATR stop under elevated realized volatility |
| `PLTR` | High-momentum growth / breakout | Dual-clamp sizing + volume gate + NASD VTS proxy |
| `AAPL` | Low-volatility large-cap control | RSI crossdown stability + crossover deduplication |

This three-asset design enables cross-regime empirical comparison: look-ahead-free ATR behavior under stress (`NVDA`), capital-clamped liquidity-filter efficacy on momentum names (`PLTR`), and signal stability on a mature benchmark (`AAPL`).

---

## License & Disclaimer

This repository is an academic quantitative infrastructure project. It connects to the **KIS Virtual Trading Server** for sandbox execution only. Past backtest performance (Phase 1–2 figures) does not guarantee future results. Always validate signals, `.env` configuration, holiday calendar behavior, and account routing independently before any real-capital deployment.
