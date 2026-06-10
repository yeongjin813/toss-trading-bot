# Multi-Asset Live Quantitative Pipeline & Risk-Resilient Infrastructure

An automated, production-grade quantitative trading infrastructure and empirical telemetry engine built on top of the Korea Investment & Securities (KIS) OpenAPI. The system ingests live market sequences across multi-regime assets, enforces strict liquidity/volatility risk gates, implements a state-gated synchronous execution lock, and programmatically neutralizes Virtual Trading Server (VTS) sandbox anomalies.

| Field | Value |
|---|---|
| **System Status** | Phase 4 Active — Production Synchronization & Memory Window Caching |
| **Watchlist Matrix** | `NVDA` (High-Vol leader), `PLTR` (Breakout growth), `AAPL` (Low-Vol control) — configurable via `.env` `WATCHLIST` |
| **Broker Gateway** | Korea Investment & Securities (KIS) OpenAPI (VTS Sandbox Deployment) |
| **Anchor Account** | CANO: `50191906` \| ACNT_PRDT_CD: `01` (Unified Portfolio Suffix) — override via `.env` |
| **Execution Loop** | 60-Second Real-Time Polling Loop with Asymmetric Network Bypass |
| **Data Architecture** | Bootstrap 756 bars once → 1-bar micro-fetch per open-session cycle |
| **Session Gate** | NYSE holiday registry + **`is_us_regular_market_hours()`** (09:30–16:00 ET via `pytz`) |
| **Execution Integrity** | Same-bar sequential ATR stop → trailing update → crossover/RSI exit |
| **Timeline Alignment** | Pre/post-market crossover blocked; ATR stop bypasses hours gate when positioned |
| **Risk Posture** | Documented in Sections 11–14 — backtest/live parity gaps, reconciliation vectors, monitoring telemetry |

**Base URL (VTS Mock):** `https://openapivts.koreainvestment.com:29443`

---

## Table of Contents

1. [Project Evolution & Development Chronology](#1-project-evolution--development-chronology)
2. [Dedicated Section: Same-Bar Look-Ahead Bias Eradication](#2-dedicated-section-same-bar-look-ahead-bias-eradication)
3. [Dedicated Section: RSI Crossdown Exit Engine](#3-dedicated-section-rsi-crossdown-exit-engine)
4. [Dedicated Section: Dual-Clamp Position Sizing](#4-dedicated-section-dual-clamp-position-sizing)
5. [Dedicated Section: NYSE Holiday Registry & Loop Suppression](#5-dedicated-section-nyse-holiday-registry--loop-suppression)
6. [System Architecture & Core Topologies](#6-system-architecture--core-topologies)
7. [Operational Hardening & Production Safeguards](#7-operational-hardening--production-safeguards)
8. [Strategy Configurations & Parameters](#8-strategy-configurations--parameters)
9. [Operational Guidelines & Verification Suite](#9-operational-guidelines--verification-suite)
10. [KIS OpenAPI Interface Gateway Reference Mapping](#10-kis-openapi-interface-gateway-reference-mapping)
11. [Core Risks & Worst-Case Operational Scenarios](#11-core-risks--worst-case-operational-scenarios)
12. [Pre-Deployment Critical Architectural Flaws & Hardening Vectors](#12-pre-deployment-critical-architectural-flaws--hardening-vectors)
13. [High-Priority Telemetry Channels (Live Monitoring Matrix)](#13-high-priority-telemetry-channels-live-monitoring-matrix)
14. [Final Operational Directives & Action Plan](#14-final-operational-directives--action-plan)

**Appendices**

- [A. Infrastructure Patch Ledger (VTS Mock API Bypasses)](#appendix-a-infrastructure-patch-ledger-vts-mock-api-bypasses)
- [B. Signal Priority & Execution Rules](#appendix-b-signal-priority--execution-rules)
- [C. Configuration Externalization Matrix](#appendix-c-configuration-externalization-matrix)
- [D. Project Structure](#appendix-d-project-structure)
- [E. Academic Regime Mapping (Watchlist Design)](#appendix-e-academic-regime-mapping-watchlist-design)
- [License & Disclaimer](#license--disclaimer)

---

## 1. Project Evolution & Development Chronology

### Phase 1: Static Backtesting Infrastructure (Baseline)

- **Architecture**: Single-ticker backtest on `AAPL` using a fixed 20-day SMA crossover over 3 years of historical daily data loaded from flat CSV files.
- **Performance**: Net return **+0.99%** — Final Portfolio Value **$10,099.42**.
- **Insight**: High vulnerability to whipsaws and capital erosion in sideways markets. A fixed lookback period without momentum or risk controls produced unreliable entry timing.

### Phase 2: Parameter Optimization & Multi-Indicator Filtering

- **Architecture**: Grid-search optimization (SMA 10–50, step 5) converged on a **10-day SMA** paired with a **14-day RSI** momentum filter (BUY when RSI >= 50; Overbought exit when RSI > 70 or SMA death cross).
- **Performance**: Net return **+0.52%** — Final Portfolio Value **$10,052.18**.
- **Insight**: Stacking uncorrelated indicators choked profitable entries (False Negative dilemma). Reduced whipsaw frequency came at the cost of missed momentum breakouts.

### Phase 3: Production-Ready Live Signal Pipeline

- **Architecture**: Replaced static CSV dependencies with automated, live API ingestion. Integrated credential encryption layers via `.env` state protection and established clean exception frameworks to process broker authorization sequences dynamically.
- **Capability**: Dynamic data persistence, standardized English console telemetry, and integrated Backtrader performance analyzers (Final Portfolio Value, Sharpe Ratio, Max Drawdown).

### Phase 4: Dynamic ATR Risk Engine & Mathematical Integrity Corrections *(Current)*

- **Architecture**: Expanded from single-asset to a parallel multi-ticker watchlist (`NVDA`, `PLTR`, `AAPL`) executing live routines through the KIS sandbox server (`openapivts.koreainvestment.com:29443`).
- **Core Engineering Patches**: Mitigated critical production failures by implementing a localized memory bar caching engine (`MarketDataCache`), a state-gated concurrency lock to eliminate duplicate order flooding, an institutional volume liquidity surge gate, and a rigorous series of financial logic patches to eliminate same-bar look-ahead bias, capital over-leverage rejections, and weekend/holiday runtime crashes.

| Upgrade | Module | Summary |
|---|---|---|
| **Same-Bar Sequential Integrity** | `analytics.py` + `strategy.py` | ATR stop vs *prior* `trigger_floor` before peak update |
| **RSI Crossdown Exit** | `analytics.py` + `strategy.py` | Verified crossdown — not blind >70 threshold |
| **Dual-Clamp Position Sizing** | `analytics.py` | Risk budget ∩ capital budget |
| **NYSE Holiday Registry** | `analytics.py` + `main.py` | Replaces naive `weekday < 5` |
| **NY Regular Hours Gate** | `analytics.py` (`pytz`) | 09:30–16:00 America/New_York; DST-safe |
| **State Persistence Fix** | `analytics.py` + `main.py` | `last_processed_date` only on executed signals |
| **Dynamic Memory Window Caching** | `main.py` → `MarketDataCache` | 756-bar bootstrap once; 1-bar micro-fetch per cycle |
| **State-Gated Order Lifecycle** | `main.py` + `analytics.py` | Dispatch → verify → lock → persist → transition |
| **Hybrid EOD / Intraday Engine** | `analytics.py` | Bar-trailing crossover gate + continuous ATR scan |
| **Liquidity Gate** | `analytics.py` + `strategy.py` | `Volume_t > Volume_SMA_20 × 1.2` for BUY |
| **Configuration Externalization** | `StrategyConfig.from_env()` | Watchlist, capital, strategy params from `.env` |

Additional Phase 4 safeguards preserved from prior milestones:

- **Exact liquidation quantity synchronization**: SELL orders clamp to `held_quantity` — never sell more than locally tracked holdings.
- **Non-blocking balance inquiry**: `try_print_mock_account_balance()` via TR `VTRP6504R` — failures logged as `[INFO]`, never block the loop.
- **PLTR NASDAQ VTS proxy**: `excd: NAS`, `ovrs_excg_cd: NASD` — bypasses 0-bar NYS truncation.
- **NY regular-hours gate**: `is_us_regular_market_hours()` blocks pre/post-market crossover; `DYNAMIC_ATR_SELL` remains active for open positions outside regular hours.
- **State persistence fix**: `last_processed_date` updates only after `BUY` / `SELL` / `DYNAMIC_ATR_SELL` order transitions — never on `HOLD`.
- **Backtest/live parity**: `strategy.py` and `analytics.py` share identical 3-step trailing sequence, RSI crossdown logic, and Backtrader `set_coc(True)`.

---

## 2. Dedicated Section: Same-Bar Look-Ahead Bias Eradication

### A. The Structural Problem

Evaluating trailing risk indicators using a state context that has already been modified by the current candle bar's close price introduces a critical mathematical flaw known as **Look-Ahead Bias**. If the system updates the trailing highest peak and tightens the floor *before* checking the intraday low breach, it implicitly uses future knowledge (the closing price of a bar that has not yet concluded), leading to severe simulation distortion and missed execution exits in live production regimes.

This bias affects both:

- **Historical backtests** — `strategy.py` / Backtrader `SmaCross.next()`
- **Live evaluation cycles** — `analytics.py` / `LiveSignalEngine.evaluate_bar()`

### B. The 3-Step In-Position Execution Sequence

To preserve chronological causality, the active position tracking loop in both `strategy.py` and `analytics.py` executes the following immutable order operations:

**Step 1: Volatility Stop Evaluation (Prior Floor Check)**

Evaluate if the current bar's intraday low breaches the trailing floor generated during the *previous* bar cycle:

$$
\text{Trigger Condition: } \text{Low}_t \le \text{Trigger Floor}_{t-1}
$$

If this expression resolves to `True`, dispatch an immediate market liquidation order to the broker and exit the execution thread immediately.

During live 60-second polling, Step 1 uses the running **session low**:

$$
\text{Liquidation Trigger (live)} \iff \text{session\_low} \le \text{Trigger Floor}_{t-1}
$$

**Step 2: Trailing State Modification (Survival Trailing)**

If and only if the position survives Step 1, accept the current bar's close price to dynamically evaluate and update the structural highest peak tracking state:

$$
\text{New Peak}_t = \max(\text{Peak}_{t-1}, \text{Close}_t)
$$

$$
\text{New Trigger Floor}_t = \text{New Peak}_t - (\text{ATR}_{14} \times 2.0)
$$

**Step 3: Alternative System Exits (Trend/Momentum Exits)**

Evaluate standard mathematical indicators (SMA Death Cross or RSI crossdown exit) using the current updated state array. RSI evaluation occurs exclusively in Step 3 — after volatility liquidation priority is resolved.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  POSITION ACTIVE — Same-Bar Sequential Integrity (No Look-Ahead)          │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 1 → Prior-floor ATR stop (Low_t ≤ Trigger Floor_{t-1})           │
│           TRUE  → DYNAMIC_ATR_SELL / self.close() → RETURN              │
│  STEP 2 → _update_trailing_state(close) — survival confirmed only       │
│  STEP 3 → Death Cross OR RSI crossdown → SELL / self.close()            │
└─────────────────────────────────────────────────────────────────────────┘
```

### C. Implementation & Test Mapping

- **`analytics.py` & `strategy.py` Parity**: Both scripts share this exact sequence inside `evaluate_bar()` and `next()`. The `trigger_floor` is explicitly persisted across state bounds (`PositionState.trigger_floor` live; `self.trigger_floor` backtest) to guarantee that intraday low scans look backward rather than forward.
- **Implementation Map**:

| Step | `analytics.py` | `strategy.py` |
|---|---|---|
| **1** | `_dynamic_atr_stop_triggered(state, effective_low)` | `_dynamic_atr_stop_triggered(bar_low)` |
| **2** | `_update_trailing_state(state, bar.close, bar.atr)` | `_update_trailing_state(close)` |
| **3** | `_death_cross()` OR `_rsi_triggers_exit(rsi, prev_rsi)` | `crossover < 0` OR `_rsi_triggers_exit()` |

- **Validation**: Enforced via `test_analytics.py` Test 3, which maps simulated bar arrays with extreme intraday drops to verify execution occurs at the prior floor boundary prior to peak adjustments:
  1. **BUY** at close 101.0 — peak and floor initialized.
  2. **HOLD** at close 110.0 — floor updated to 106.0 only after surviving Step 1.
  3. **DYNAMIC_ATR_SELL** when bar low (105.50) ≤ prior floor (106.00).

---

## 3. Dedicated Section: RSI Crossdown Exit Engine

### A. Paradigm Shift: Crossdown vs. Fixed Threshold

Exiting a trend instantly when $\text{RSI}_t > 70$ causes severe profit truncation in momentum-driven assets (e.g., `NVDA` or `PLTR`), as high-velocity growth equities frequently cluster inside overbought regimes during major macro breakouts. The pipeline implements a **Crossdown Mode** (default), which permits the position to run while RSI remains extended, triggering liquidation only when momentum structurally decelerates and breaks back below the upper envelope.

An optional **Threshold Mode** (`RSI_EXIT_MODE=threshold`) exits when $\text{RSI}_t > 70$ — non-default, aggressive configuration.

### B. Explicit Mathematical Formula

The momentum liquidation signal activates if and only if the previous session's momentum index was anchored at or above the threshold boundary while the current session closes strictly below it:

$$
\text{Signal Code (SELL)} \longleftrightarrow \left( \text{RSI}_{t-1} \ge 70 \right) \;\wedge\; \left( \text{RSI}_t < 70 \right)
$$

RSI sustained above 70 without crossing down does **not** trigger an exit.

| Mode | Env Variable | Condition |
|---|---|---|
| **crossdown** *(default)* | `RSI_EXIT_MODE=crossdown` | $\text{RSI}_{t-1} \ge \text{upper\_limit}$ AND $\text{RSI}_t < \text{upper\_limit}$ |
| **threshold** | `RSI_EXIT_MODE=threshold` | $\text{RSI}_t > \text{upper\_limit}$ |

Default `upper_limit = 70` (override via `RSI_UPPER_LIMIT` in `.env`).

### C. Implementation Syntax

Synchronized across both live evaluation layers and Backtrader strategy scripts:

**`strategy.py` — `SmaCross._rsi_triggers_exit()`**

```python
# Synchronized across both live evaluation layers and Backtrader strategy scripts
return (
    self.rsi[-1] >= self.params.rsi_sell_threshold
    and self.rsi[0] < self.params.rsi_sell_threshold
)
```

**`analytics.py` — `LiveSignalEngine._rsi_triggers_exit()`**

```python
def _rsi_triggers_exit(self, rsi: float, prev_rsi: float) -> bool:
    upper = self.config.rsi_upper_limit
    if self.config.rsi_exit_mode == "threshold":
        return rsi > upper
    return prev_rsi >= upper and rsi < upper
```

---

## 4. Dedicated Section: Dual-Clamp Position Sizing

### A. Theoretical Vulnerability

Calculating order execution quantity based purely on risk allocation ($\text{shares} = \lfloor \text{Dollar Risk} / \text{Stop Distance} \rfloor$) creates a critical failure point when dealing with low-volatility or high-nominal-price assets (e.g., trading `PLTR` with a tight ATR trailing stop). If the volatility buffer is small, the mathematical risk engine scales share volume exponentially, resulting in order allocations that exceed the total account balance, prompting immediate broker rejections (Insufficient Funds).

### B. Dual-Clamp Constraints Formula

To guarantee real-world execution safety, the sizing logic inside `calculate_position_size()` in `analytics.py` applies a strict dual-clamp optimization loop:

$$
\text{shares\_by\_risk} = \left\lfloor \frac{\text{Capital At Risk} \times \text{Risk Per Trade}}{\text{ATR}_{14} \times \text{ATR Multiplier}} \right\rfloor
$$

$$
\text{shares\_by\_capital} = \left\lfloor \frac{\text{Available Local Capital}}{\text{Current Asset Price}} \right\rfloor
$$

$$
\text{Final Allocated Execution Shares} = \max\left(1,\; \min\left(\text{shares\_by\_risk},\; \text{shares\_by\_capital}\right)\right)
$$

**Guard Condition**: If either calculation yields a non-positive value, the engine returns an absolute allocation score of **$0$** to abort dangerous market order execution pipelines.

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

### C. Worked Empirical Example (PLTR)

**Account Parameter Constraints:** $\text{Capital At Risk} = \$10{,}000$ | $\text{Risk Per Trade} = 1\%$ ($\$100$ Max Loss).

**Asset Market Conditions:** $\text{Current Price} = \$250$ | $\text{ATR}_{14} = 1.0$ | $\text{Multiplier} = 2.0$.

**Execution Sizing Ingestion Mechanics:**

$$
\text{shares\_by\_risk} = \frac{\$100}{1.0 \times 2.0} = 50 \text{ shares}
$$

$$
\text{Required Capital for Risk Leg} = 50 \text{ shares} \times \$250 = \$12{,}500 \quad (\text{EXCEEDS } \$10{,}000 \text{ BASE!})
$$

$$
\text{shares\_by\_capital} = \frac{\$10{,}000}{\$250} = 40 \text{ shares}
$$

$$
\text{Final Allocation} = \min(50,\; 40) = 40 \text{ shares} \quad \rightarrow \quad \text{Execution Budget Secured at } \$10{,}000.
$$

---

## 5. Dedicated Section: NYSE Holiday Registry & Loop Suppression

### A. System Crash Vector

Executing automated, high-frequency broker loops on weekends or recognized exchange holidays leads to systemic data degradation. The KIS OpenAPI gateway responds to requests during closures with fatal error states, unmapped null matrices, or connection drops, triggering immediate runtime exceptions (`AttributeError`, `KeyError`) that crash unshielded scripts.

The legacy gate (`weekday < 5`) treated US market holidays as normal trading days, wasting API quota and risking network-layer instability.

### B. Hardened Registry Calibration Table

The orchestration engine completely removes basic `weekday < 5` date processing as the sole gate, routing all execution loops through a localized calculation grid covering the six core NYSE equity holidays via `us_market_holidays_for_year()` in `analytics.py`:

| Holiday | Observed Rule / Calculation Routine | Closure Loop Mode Behavior |
|---|---|---|
| **New Year's Day** | January 1st (If Saturday → Friday Dec 31; If Sunday → Monday January 2nd) | Full Loop Bypass & Sleep |
| **Memorial Day** | Last Monday occurring within the month of May | Full Loop Bypass & Sleep |
| **Independence Day** | July 4th (If Saturday → Friday Jul 3; If Sunday → Monday Jul 5) | Full Loop Bypass & Sleep |
| **Labor Day** | First Monday occurring within the month of September | Full Loop Bypass & Sleep |
| **Thanksgiving** | Fourth Thursday occurring within the month of November | Full Loop Bypass & Sleep |
| **Christmas Day** | December 25th (If Saturday → Friday Dec 24; If Sunday → Monday Dec 26) | Full Loop Bypass & Sleep |

#### Core Calendar Functions

| Function | Module | Purpose |
|---|---|---|
| `us_market_holidays_for_year(year)` | `analytics.py` | Returns `{date: holiday_name}` registry |
| `is_us_market_holiday(day)` | `analytics.py` | Boolean holiday check |
| `describe_us_market_closure(now)` | `analytics.py` | Returns `"weekend"`, holiday name, or `None` (NY calendar date) |
| `is_us_equity_session(now)` | `analytics.py` | Calendar open: NY weekday excluding holidays |
| `is_us_regular_market_hours(now)` | `analytics.py` | Regular session: Mon–Fri 09:30–16:00 **America/New_York** via `pytz` |
| `_to_ny_datetime(now)` | `analytics.py` | DST-safe conversion to NY time (no hardcoded KST offset) |

Weekends and holidays are evaluated on the **New York local calendar date** inside `describe_us_market_closure()`.

### D. Regular Market Hours Gate (Pre/Post-Market Crossover Block)

The calendar gate (`is_us_equity_session`) and the **regular-hours gate** (`is_us_regular_market_hours`) operate as two distinct layers:

| Layer | Function | Scope |
|---|---|---|
| **Calendar** | `is_us_equity_session()` | NY weekday, not a registered holiday |
| **Regular Hours** | `is_us_regular_market_hours()` | Calendar open **AND** 09:30 ≤ clock < 16:00 ET |

```python
import pytz

NY_TZ = pytz.timezone("America/New_York")

def is_us_regular_market_hours(current_dt=None) -> bool:
    ny_dt = _to_ny_datetime(current_dt)  # DST-safe via pytz
    if ny_dt.weekday() >= 5 or ny_dt.date() in us_market_holidays_for_year(ny_dt.year):
        return False
    return time(9, 30) <= ny_dt.time() < time(16, 0)
```

**Pre-market / post-market behavior:**

- `should_allow_crossover_signals(..., regular_market_hours=False)` → **Crossover Allowed = False**
- Default signal outside regular hours (no position): **`HOLD`** — `BUY`/`SELL` blocked in `main.py`
- **`DYNAMIC_ATR_SELL`**: Still evaluated when `_has_active_position()` is true — **bypasses** the regular-hours gate for flash-crash protection

### E. Extended Sleep Logic Behavior

When `is_us_market_holiday()` or weekend validations return `True`, the orchestrator in `main.py` completely suspends asset data fetching. It short-circuits execution and puts the master thread into an extended standby mode controlled by:

| Variable | Default | Description |
|---|---|---|
| `MARKET_CLOSED_SLEEP_SECONDS` | **3600** (1 hour) | Configurable via `.env` |

```
--- Cycle N skipped at 2026-12-25 09:00:00 ---
[GATE] US market closed (Christmas). Sleeping 3600 seconds...
```

During NY calendar-open sessions, `LOOP_COOLDOWN_SECONDS = 60` applies. On **weekends and holidays**, the full cycle skip prevents all broker queries. On **weekday pre/post-market**, the loop still runs for **positioned ATR stop** monitoring while crossover remains suppressed.

---

## 6. System Architecture & Core Topologies

### Multi-Asset Tracking Pipeline Matrix

```
                         ┌─────────────────────────────────────────────┐
                         │             main.py — Orchestrator          │
                         │         Watchlist: [NVDA, PLTR, AAPL]       │
                         └─────────────────────┬───────────────────────┘
                                               │
              ┌────────────────────────────────┼────────────────────────────────┐
              │                                │                                │
              ▼                                ▼                                ▼
     ┌─────────────────┐              ┌─────────────────┐              ┌─────────────────┐
     │  NVDA Pipeline  │              │  PLTR Pipeline  │              │  AAPL Pipeline  │
     ├─────────────────┤              ├─────────────────┤              ├─────────────────┤
     │  KIS API Fetch  │              │ KIS API Proxy   │              │  KIS API Fetch  │
     │ → nvda_daily.csv│              │ (NYS → NASD)    │              │ → aapl_daily.csv│
     │  analytics.py   │              │ analytics.py    │              │  analytics.py   │
     │  Live Signal    │              │ Live Signal     │              │  Live Signal    │
     │  Backtrader BT  │              │ Backtrader BT   │              │  Backtrader BT  │
     └─────────────────┘              └─────────────────┘              └─────────────────┘
              │                                │                                │
              └────────────────────────────────┼────────────────────────────────┘
                                               ▼
                         ┌─────────────────────────────────────────────┐
                         │   [METRICS] Stream Output / Redirect Pipe   │
                         └─────────────────────────────────────────────┘
```

### Core Subsystem Mapping

| Layer | Module | Responsibility |
|---|---|---|
| **Orchestration** | `main.py` | Secure environment loading, KIS authentication token provisioning (`kis_token_cache.json`), 60s watchlist sequencing, NYSE holiday gate, order transmission execution, graceful crash state serialization |
| **Indicators & Logic** | `analytics.py` | Houses `LiveSignalEngine` — rolling historical buffers, Wilder-smoothed metrics, dual-clamp sizing, 3-step bar evaluation, NYSE calendar, `PositionState` lifecycle |
| **Strategy Sandbox** | `strategy.py` | Identical backtesting twin utilizing Backtrader; annualized daily Sharpe analyzers; transaction friction rules; look-ahead-free trailing stop parity |
| **Persistence Layer** | `data/{ticker}_daily.csv`, `trading_state.json` | Local cache boundaries and active holding state registry |

### Strategic Parameter Telemetry Disconnect

- **Execution Signal Engine**: Evaluates entries using $\text{SMA}_{10}$ via `StrategyConfig` for higher breakout sensitivity.
- **Telemetry Logging Layer**: Independent extraction of $\text{SMA}_{20}$ (`ANALYTICS_SMA_PERIOD = 20` in `main.py`) for external regression and confusion-matrix modeling — intentional disconnect for empirical visualization.

### Runtime State Registry (`trading_state.json`)

| Field | Type | Purpose |
|---|---|---|
| `in_position` | `bool` | Active position flag (`has_position` property) |
| `pending_order` | `bool` | Concurrency lock during in-flight orders |
| `held_quantity` | `int` | Share count for exact liquidation clamping |
| `last_processed_date` | `str \| null` | Set **only** after `BUY`/`SELL`/`DYNAMIC_ATR_SELL` order transition — never on `HOLD` |
| `latest_bar_date` | `str \| null` | Active daily bar for session-low tracking |
| `session_low` | `float \| null` | Running min low on active daily bar |
| `highest_price_achieved` | `float \| null` | Peak close since entry |
| `trigger_floor` | `float \| null` | **Prior-bar** floor for Step 1 stop check |
| `current_atr` | `float \| null` | Latest Wilder ATR(14) |
| `dynamic_stop_distance` | `float \| null` | `ATR × atr_multiplier` |

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

### KIS Exchange Cross-Regime Routing Table (`MARKET_META`)

To bypass severe database replication lag on the KIS Virtual Trading Server (VTS), where querying standard NYSE configurations (`NYS`) for high-momentum targets like `PLTR` yields truncated empty matrices ($0$ historical bars cached), the engine uses explicit exchange routing overrides in `main.py`:

| Ticker | Experimental Regime Role | Price Feed Code (`excd`) | Order Execution Code (`ovrs_excg_cd`) |
|---|---|---|---|
| `NVDA` | High-Volatility Realized Momentum | `NAS` | `NASD` |
| `PLTR` | High-Momentum Breakout Validation | `NAS` (Forced NASDAQ Proxy) | `NASD` (Forced NASDAQ Proxy) |
| `AAPL` | Low-Volatility Large-Cap Control Baseline | `NAS` | `NASD` |

New watchlist tickers must be registered in `MARKET_META` before being added to `WATCHLIST` in `.env`.

### Continuous Monitoring Loop

```
validate_environment()
  → KIS OAuth token (kis_token_cache.json)
  → MarketDataCache.bootstrap()          # 756 bars × tickers, once
  → load trading_state.json
  → try_print_mock_account_balance()     # VTRP6504R — non-blocking
  → log_configured_capital_model()
  → while True:
        IF describe_us_market_closure() != None:
            sleep(MARKET_CLOSED_SLEEP_SECONDS)   # 3600 default
            continue
        run_watchlist_cycle()
        sleep(LOOP_COOLDOWN_SECONDS)             # 60 default
```

---

## 7. Operational Hardening & Production Safeguards

### A. Network Traffic Optimization (`MarketDataCache`)

To prevent the system from getting flagged or blacklisted by the broker's rate limiter (Rate Limit / Traffic Throttle) due to downloading 3 years of daily historical data ($756 \text{ bars}$) per ticker every 60 seconds, the engine deploys a localized window-caching pattern in `main.py`:

**Bootstrap Ingestion:** On initialization, the system downloads the full historical data slice ($756$ bars) exactly once to fill the in-memory buffer and persists to `data/{ticker}_daily.csv`.

**Micro-Fetch Window Loop:** In subsequent 60-second loops, the engine requests only the single latest active bar from KIS (TR `HHDFS76240000`, one page), validates its structure, and appends or in-place updates the localized DataFrame matrix.

| Constant | Value | Role |
|---|---|---|
| `LOOKBACK_YEARS` | 3 | Historical depth |
| `TARGET_BARS` | 756 | Rolling window cap |
| `MIN_DATA_BARS` | 22 | Minimum before analytics run |

| Mode | API Calls per Cycle (3 tickers) | Bars Transferred (approx.) |
|---|---|---|
| **Pre-Phase-4** | 3 × ~15 paginated requests | ~2,268 bars |
| **Phase-4** | 3 × 1 request | ~3 bars |

### B. State-Gated Synchronous Order Lifecycle Chain

The system enforces a strict state boundary to prevent duplicate order generation or "double-buying" bugs caused by identical EOD signals evaluating inside a 60-second polling window:

$$
\text{KIS Order Dispatch} \longrightarrow \text{Verify Response (rt\_cd == "0")} \longrightarrow \text{Lock local pending\_order = True} \longrightarrow \text{Commit state to disk} \longrightarrow \text{apply\_post\_order\_transition()} \longrightarrow \text{Final persist}
$$

While `pending_order` resolves to `True`, all incoming execution signals for that specific ticker are suppressed and ignored until the local state machine confirms transaction fulfillment via `apply_post_order_transition()`.

| Step | Action | Module |
|---|---|---|
| **0** | `pending_order == True` → return `LOCKED` | `main.py` |
| **1** | `_resolve_execution_quantity()` | `main.py` |
| **2** | KIS dispatch + hashkey | `main.py` |
| **3** | Verify `rt_cd == "0"` | `main.py` |
| **4–5** | Lock + interim save (crash protection) | `main.py` |
| **6–7** | Transition + final persist | `analytics.py` + `main.py` |

### C. Exact Liquidation Quantity Clamping

Before generating any `SELL` or `DYNAMIC_ATR_SELL` execution payload, the engine invokes `_resolve_execution_quantity()`. This helper queries the actual open position tracking data from the persisted state and hard-clamps the execution quantity:

```
execution_quantity = min(proposed_size, held_quantity)
```

If `held_quantity <= 0`, liquidation is skipped — avoiding broker rejections caused by trying to sell more shares than are locally available.

### D. Hybrid EOD / Intraday Monitoring Logic

**`should_allow_crossover_signals()` Integration:** Crossover `BUY`/`SELL` requires **`regular_market_hours=True`** (09:30–16:00 ET) **and** a new daily bar (`current_bar_date != last_processed_date`). Pre-market and post-market force crossover off.

**`last_processed_date` Persistence Rule:** Updated exclusively inside `apply_post_order_transition()` after a confirmed KIS order for `BUY`, `SELL`, or `DYNAMIC_ATR_SELL`. The removed `mark_crossover_processed()` path no longer locks the date on `HOLD` — preventing regular-session signals from being swallowed after a pre-market evaluation cycle.

**Continuous Intraday Risk Scan:** Priority 1 `DYNAMIC_ATR_SELL` runs only when `_has_active_position()` (`in_position` or `held_quantity > 0`):

$$
\text{Low}_t \le \text{Trigger Floor}_{t-1}
$$

| Condition | Crossover BUY/SELL | DYNAMIC_ATR_SELL |
|---|---|---|
| Regular hours + new daily bar | **Allowed** | **Active** (if positioned) |
| Same daily bar | **Suppressed** | **Active** (`session_low`) |
| Pre-market / post-market (weekday) | **Suppressed** → `HOLD` | **Active** (if positioned) |
| Weekend or NYSE holiday | **Suppressed** | Full cycle skipped |
| `pending_order == True` | **Locked** | **Locked** |

### E. Liquidity Gate Calibration

Long entry (`BUY`) requires institutional volume surge confirmation:

```
Volume Gate PASS:  Volume_t > Volume_SMA_20 × 1.2
Volume Gate FAIL:  Volume_t ≤ Volume_SMA_20 × 1.2  →  HOLD (liquidity_blocked)
```

Implemented identically in `analytics.py` and `strategy.py`.

---

## 8. Strategy Configurations & Parameters

All defaults overridable via `.env` and `StrategyConfig.from_env()` (see [Appendix C](#appendix-c-configuration-externalization-matrix)).

| Parameter | Operational Assignment Value | Functional Description Reference |
|---|---|---|
| `sma_period` | **10** | Moving average window for trend breakout execution signals |
| `ANALYTICS_SMA_PERIOD` | **20** | Independent baseline window for `[METRICS]` logging disconnect |
| `rsi_period` | **14** | Lookback window mapping momentum calculations (Wilder smoothing) |
| `rsi_buy_threshold` | **50** | Minimum momentum score required to clear entry authorization |
| `rsi_sell_threshold` / `rsi_upper_limit` | **70** | Structural ceiling threshold mapping Crossdown exit evaluations |
| `rsi_exit_mode` | **crossdown** | Verified crossdown — not blind >70 threshold |
| `atr_period` | **14** | Lookback window smoothing true range variance arrays |
| `atr_multiplier` | **2.0** | Volatility standard multiplier for dynamic floor boundaries |
| `volume_sma_period` | **20** | Institutional liquidity baseline window mapping |
| `volume_threshold` | **1.2** | Multiplier requiring a 20% volume surge above baseline for BUY signals |
| `use_trailing_stop` | **true** | Enable dynamic ATR trailing stop (Step 1 prior-floor check) |
| `commission_rate` | **0.001** | Transaction friction constant applied to backtest runs (0.1%) |
| `min_data_bars` | **22** | Minimum baseline lookback window required to activate processing loops |
| `CAPITAL_AT_RISK` | **10000** | Deployable capital for dual-clamp sizing (USD) |
| `RISK_PER_TRADE` | **0.01** | Per-trade risk fraction (1%) |
| `TARGET_BARS` | **756** | Rolling historical window (3 × 252 trading days) |
| `LOOP_COOLDOWN_SECONDS` | **60** | Open-session inter-cycle pause |
| `MARKET_CLOSED_SLEEP_SECONDS` | **3600** | Closed-session extended sleep |
| `TICKER_SLEEP_SECONDS` | **1** | Pause between tickers within a cycle |

---

## 9. Operational Guidelines & Verification Suite

### A. Environment Initialization (`.env`)

Create a localized state configuration file named `.env` in the project root directory. Ensure it is listed within your `.gitignore` to prevent leaking active portal credentials to public repositories:

```ini
# KIS OpenAPI VTS Sandbox Credentials
KIS_APP_KEY=your_app_key_here
KIS_APP_SECRET=your_app_secret_here
KIS_CANO=50191906
KIS_ACNT_PRDT_CD=01

# Production Strategic Risk Configuration Parameters
WATCHLIST=NVDA,PLTR,AAPL
CAPITAL_AT_RISK=10000
RISK_PER_TRADE=0.01
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

The system automatically triggers `load_dotenv(override=True)` to ensure localized parameter configurations take absolute precedence over stale shell environments.

Copy from `.env.example` as a starting template. **Never commit `.env` to version control.**

### B. Dependency Installation

```powershell
pip install -r requirements.txt
```

```
backtrader
matplotlib
numpy
pandas
python-dotenv
pytz
requests
yfinance
```

### C. Launch Continuous Monitor

```powershell
python main.py
```

Expected startup sequence:

1. `validate_environment()` — KIS credentials + `MARKET_META` routing
2. KIS OAuth token (cached in `kis_token_cache.json`)
3. `MarketDataCache.bootstrap()` — ~756 bars per ticker, once
4. Load `trading_state.json`
5. `try_print_mock_account_balance()` — optional, non-blocking
6. `log_configured_capital_model()` — confirms `CAPITAL_AT_RISK`
7. Infinite watchlist loop with `[METRICS]` emission

Expected runtime log markers:

```
Order pipeline: KIS dispatch -> rt_cd verify -> state transition -> persist
--- Cycle 12 skipped at 2026-12-25 09:00:00 ---
[GATE] US market closed (Christmas). Sleeping 3600 seconds...
[GATE] Outside NY regular hours (09:30–16:00 ET) — crossover suppressed; ATR stop active for open positions
[GATE] NVDA outside NY regular session (09:30–16:00 ET) — BUY blocked, holding
[LOCK] NVDA pending_order=True — signal suppressed
[SKIP] PLTR liquidation skipped — no held quantity tracked
  -> 756 bars in memory (intraday refresh)
  -> Order accepted (rt_cd=0). State transition applied for NVDA: has_position=True, held_qty=12
```

### D. Empirical Log Redirection Command

To operate the pipeline in a clean background configuration while piping telemetry records into an isolated analytics log for visualization processing:

**PowerShell:**

```powershell
python main.py *> project_metrics.log
```

**Bash:**

```bash
python main.py > project_metrics.log 2>&1
```

### E. Sample Pipe-Delimited Telemetry Output (`[METRICS]`)

```
[METRICS] NVDA | 2026-06-10 15:45:02 | Close=142.50 | SMA20=138.20 | RSI=58.32 | ATR_Wilder=4.2150 | Volume=45,230,000/38,100,000 (1.187x)
[METRICS] PLTR | 2026-06-10 15:45:03 | Close=254.10 | SMA20=241.15 | RSI=71.20 | ATR_Wilder=5.1200 | Volume=18,900,000/12,100,000 (1.562x)
```

Extended per-cycle telemetry fields:

```
Bar Date             : 2026-06-05
Session Low          : 138.4200
Calendar Open        : True
Regular Market Hours : False
Crossover Allowed    : False
Signal               : HOLD
Position Size (shares): 40
Runtime Registry     : pending=False | held_qty=40 | last_processed=2026-06-05
ATR Stop Telemetry   : peak=$145.80 | floor=$137.37 | session_low=$138.42
```

### F. Verification Suite

```powershell
python test_analytics.py
```

Runs unit tests against `LiveSignalEngine`:

- Wilder RSI/ATR/SMA indicator computation (Test 1)
- O(1) state replay determinism (Test 2)
- **3-step bar transitions** — look-ahead-free DYNAMIC_ATR_SELL (Test 3)
- External state serialization round-trip (Test 4)

### G. Graceful Thread Termination

To halt the continuous polling routine safely, dispatch a SIGINT termination signal via **Ctrl + C**. The orchestrator intercepts the termination hook, finishes the active processing sequence, and dumps the transactional matrix to `./trading_state.json` to prevent data loss.

---

## 10. KIS OpenAPI Interface Gateway Reference Mapping

| Interface Operational Name | Broker Target TR ID | Resource HTTP Routing Path | Core System Dependency Bind |
|---|---|---|---|
| OAuth Token Issuance | — | `POST /oauth2/tokenP` | Cached in `kis_token_cache.json` |
| Inquire Present Balance | `VTRP6504R` | `/uapi/overseas-stock/v1/trading/inquire-present-balance` | `try_print_mock_account_balance()` [Optional/Non-blocking] |
| US Daily Price Quotations (full) | `HHDFS76240000` | `/uapi/overseas-price/v1/quotations/dailyprice` | `MarketDataCache` bootstrap (~756 bars) |
| US Daily Price Quotations (latest) | `HHDFS76240000` | `/uapi/overseas-price/v1/quotations/dailyprice` | `MarketDataCache.refresh_latest()` (1 page/cycle) |
| Overseas Order Placement (Buy) | `VTTT1002U` | `/uapi/overseas-stock/v1/trading/order` | Execution signal routing — BUY |
| Overseas Order Placement (Sell) | `VTTT1006U` | `/uapi/overseas-stock/v1/trading/order` | Execution signal routing — SELL / DYNAMIC_ATR_SELL |
| Order Hashkey Validation | — | `POST /uapi/hashkey` | Required before every order transmission |

**Deprecated / Invalid on VTS:** `VTTT3402R` (balance inquiry) — returns `OPSQ0002`. Use `VTRP6504R`.

**Base URL (VTS Mock):** `https://openapivts.koreainvestment.com:29443`

---

## 11. Core Risks & Worst-Case Operational Scenarios

This section documents **residual production risks** that persist even after Phase 4 mathematical corrections (Sections 2–5). These scenarios define the boundary conditions under which backtested performance diverges from live realized returns.

### A. Intraday Flash Crash Tracking Error (Backtest vs. Live Parity Failure)

- **The Core Risk**: In the event of an intraday flash crash under elevated volatility regimes, the live simulation engine (`analytics.py`) tracks the continuous session low sequence via `update_session_low()` and fires an immediate `DYNAMIC_ATR_SELL` signal to liquidate assets at market price. However, the historical backtesting twin (`strategy.py`) evaluates price bounds exclusively on an End-of-Day (EOD) daily candle vector ($\text{Low}_t$).
- **Worst-Case Scenario**: Intraday stop liquidations will trigger immediately in real-world trading, whereas the backtest remains completely blind to intra-candle volatility path-dependency. This divergence creates massive **Slippage** and structural **Tracking Error**, yielding actual historical returns that may deviate by over **20%** from the backtested baseline.

| Dimension | Live Engine (`analytics.py`) | Backtest Twin (`strategy.py`) |
|---|---|---|
| Stop price path | Running `session_low` (60s polling) | Static daily `Low_t` only |
| Trigger timing | Intraday, potentially mid-bar | End-of-bar EOD evaluation |
| Parity status | **Asymmetric** — live is strictly more reactive |

### B. Execution Timing Mismatch (The One-Day Latency Trap)

- **The Core Risk**: By default, Backtrader's execution engine dispatches market actions (`self.close()`) to execute on the **Next Day's Open (Next Open)** price. Conversely, the live orchestration architecture (`analytics.py`) calculates portfolio net asset value (NAV) assuming immediate execution at the **Current Day's Close (EOD Close)**:

$$
\text{Backtest Execution Price} = \text{Open}_{t+1}
$$

$$
\text{Live Engine Execution Price} = \text{Close}_t
$$

- **Operational Malfunction**: This structural 24-hour execution delta guarantees that live execution will drift into immediate anti-alignment (off-beat entries and exits), severely degrading actual realized performance compared to optimized backtests.

| Execution Assumption | Module | Price Anchor |
|---|---|---|
| Next-bar open fill | `strategy.py` (Backtrader default) | $\text{Open}_{t+1}$ |
| Same-bar close fill | `analytics.py` / `main.py` live loop | $\text{Close}_t$ |
| Recommended alignment | Backtrader `set_coc(True)` | Cheat-on-Close → $\text{Close}_t$ (see Section 14) |

---

## 12. Pre-Deployment Critical Architectural Flaws & Hardening Vectors

The following items represent **known structural vulnerabilities** in the current codebase. Sections 2–7 document implemented safeguards; this section defines the **remaining hardening vectors** required before real-capital deployment.

### ① Virtual Position State Desynchronization (Reconciliation Vulnerability)

- **The Defect**: The `LiveSignalEngine` relies entirely on a localized virtual memory registry (`PositionState` in `trading_state.json`) to determine position ownership limits (`in_position`, `held_quantity`). If a live order transmission encounters an API rate-limit breach, connection timeout, or partial execution failure at the broker level, the local file will desynchronize from reality.
- **The Hardening Patches**: Incorporate a strict **Portfolio Reconciliation Layer** prior to cycle execution. The engine must query broker account states directly via the KIS API to cross-examine actual held share volume against local registries:

$$
\Delta \text{Shares} = \text{Broker Held Quantity} - \text{Local Persisted Quantity}
$$

If $\Delta \text{Shares} \neq 0$, the machine must freeze order execution threads, force-reconcile the local state matrix to match the broker asset registry, and issue an emergency log warning.

| Reconciliation Source | KIS Interface | TR ID |
|---|---|---|
| Broker held quantity | Inquire present balance / holdings | `VTRP6504R` |
| Local persisted quantity | `trading_state.json` → `held_quantity` | — |

### ② All-In Leverage Capital Erosion Buffer (Fee & Slippage Leakage)

- **The Defect**: The asset calculation string `shares_by_capital = int(deployable / entry_price)` inside `calculate_position_size()` assumes absolute zero-friction transactions. When deploying an aggressive 100% "all-in" capital allocation model, the omission of exchange fees, local broker taxes, and real-time execution slippage will cause order volumes to calculate slightly above available liquidation thresholds.
- **The Hardening Patches**: Implement a strict **Capital Allocation Safety Buffer** inside `calculate_position_size()`. Force an operational haircut to restrict total immediate deployment capital to exactly 95%–98% of liquid buying power, or dynamically compute transaction fees prior to rounding down integer components:

$$
\text{Adjusted Available Capital} = \text{Liquid Capital} \times 0.95
$$

| Current Behavior | Proposed Hardening |
|---|---|
| `deployable = capital_at_risk` (100%) | `deployable = capital_at_risk × CAPITAL_DEPLOY_BUFFER` (0.95–0.98) |
| No fee deduction in sizing | Pre-deduct estimated commission + slippage reserve |

### ③ Intraday vs. EOD Timeframe Non-Invertibility

- **The Defect**: The live script logs real-time macro-ticks through `update_session_low()`, while the backtester utilizes uniform flat daily candles. Evaluating real-time intraday tick feeds against an EOD daily bar backtest is mathematically invalid for path-dependent stop logic.
- **The Hardening Patches**: Establish absolute structural clarity. If the system executes as a true real-time engine, the backtesting infrastructure inside `strategy.py` must be entirely refactored to ingest high-frequency 1-minute or 5-minute intraday bars instead of daily vectors to preserve structural consistency — **or** the live engine must downgrade to pure EOD evaluation (eliminating `session_low` intraday scanning).

| Mode | Data Granularity | Stop Path Fidelity |
|---|---|---|
| Current live | Daily bar + `session_low` overlay | Intraday reactive |
| Current backtest | Daily OHLCV only | EOD reactive |
| Target parity | Matched granularity (1m/5m or pure EOD) | Invertible |

### ④ Hardcoded Holiday Computation Risk

- **The Defect**: While mathematical calculation helpers like `_memorial_day()` and `_thanksgiving_day()` resolve floating holiday offsets dynamically, they cannot catch emergency market adjustments (e.g., unexpected national mourning closures) or volatile lunisolar dates like **Good Friday**.
- **The Hardening Patches**: Integrate Python's `holidays` library mapped to the `NYSE` market definition, forcing a multi-layer cross-check against the internal arithmetic calendar to shield the network interface from processing payloads on closure dates:

```python
import holidays
nyse_calendar = holidays.NYSE(years=range(current_year - 1, current_year + 2))
# Cross-check: if date in nyse_calendar AND is_us_market_holiday(date) → sleep
# If nyse_calendar marks closed but internal registry misses → log CRITICAL + sleep
```

---

## 13. High-Priority Telemetry Channels (Live Monitoring Matrix)

To prevent catastrophic pipeline failure, the monitoring terminal must visualize and audit the following pipe-delimited diagnostic variables across every cycle. These channels extend the existing `[METRICS]` stream (Section 9) with **production-grade failure detection**.

| # | Telemetry Channel | Computation / Source | Failure Threshold | Required Action |
|---|---|---|---|---|
| **1** | `real_vs_local_position_mismatch` | $\Delta \text{Shares} = \text{Broker Qty} - \text{Local held\_quantity}$ | $\Delta \neq 0$ | Auto-terminate cron schedule; freeze order threads; force reconcile |
| **2** | `api_response_time` | Round-trip latency per KIS HTTP call (ms) | $> 3000\text{ ms}$ | Trigger timeout exemption; append payload to Retry Queue |
| **3** | `trigger_floor_distance` | $\text{Close}_t - \text{Trigger Floor}_t$ | Approaching $\leq 0$ | Elevated liquidation proximity alert; log ATR regime shift |

### Sample Extended `[METRICS]` Line (Target Schema)

```
[METRICS] NVDA | 2026-06-10 15:45:02 | Close=142.50 | trigger_floor_distance=5.13 |
real_vs_local_position_mismatch=0 | api_response_time=842ms | Signal=HOLD
```

### Integration Points

| Channel | Inject Location | Status |
|---|---|---|
| `real_vs_local_position_mismatch` | Pre-cycle reconciliation in `main.py` | **Planned** (Section 14) |
| `api_response_time` | `KISApiClient` request wrapper | **Planned** |
| `trigger_floor_distance` | `print_session_telemetry()` in `main.py` | Partially available via ATR Stop Telemetry block |

Existing per-cycle fields already emitted (Section 9):

```
ATR Stop Telemetry   : peak=$145.80 | floor=$137.37 | session_low=$138.42
Runtime Registry     : pending=False | held_qty=12 | last_processed=2026-06-05
```

Derive `trigger_floor_distance` as:

$$
\text{trigger\_floor\_distance} = \text{Close}_t - \text{Trigger Floor}_t
$$

---

## 14. Final Operational Directives & Action Plan

> **Structural Verdict**: The mathematical logic has achieved **structural integrity** (Sections 2–5), but deploying real capital without aligning execution timing boundaries and hard infrastructure failure walls will result in rapid capital degradation.

### Immediate Execution Priority Tasks

| Priority | Task | Target Module | Implementation Directive |
|---|---|---|---|
| **P0** | Backtest Timing Alignment | `strategy.py` / Backtrader init | Inject `self.cerebro.broker.set_coc(True)` (Cheat-on-Close) into the Backtrader initialization block. Forces backtester to execute transactions on the current bar's close price, synchronizing mathematical alignment with `analytics.py` ($\text{Close}_t$). |
| **P0** | Portfolio Reconciliation Layer | `main.py` | Query KIS held quantity via `VTRP6504R` before each cycle; compute $\Delta \text{Shares}$; freeze on mismatch (Section 12①). |
| **P1** | Capital Deploy Buffer | `analytics.py` | Apply $\text{Adjusted Capital} = \text{Liquid Capital} \times 0.95$ in `calculate_position_size()` (Section 12②). |
| **P1** | NYSE Calendar Cross-Check | `analytics.py` | Integrate `holidays.NYSE` multi-layer validation (Section 12④). |
| **P2** | API Exception Handling Queue | `main.py` | Build standardized asynchronous retry queue for failed order transmissions. |
| **P2** | Telegram Alert Webhook | `main.py` | Automated alert infrastructure for unmapped broker failures, $\Delta \text{Shares} \neq 0$, and `api_response_time > 3.0s`. |
| **P3** | Intraday Backtest Parity | `strategy.py` | Refactor to 1-minute / 5-minute bars **or** downgrade live to pure EOD (Section 12③). |

### Cheat-on-Close Reference Implementation

```python
import backtrader as bt

cerebro = bt.Cerebro()
cerebro.broker.set_coc(True)   # Execute at current bar close — aligns with analytics.py EOD assumption
# cerebro.broker.setcommission(commission=0.001)  # 0.1% friction — match Section 8
```

### Pre-Deployment Checklist

- [ ] Backtrader `set_coc(True)` verified — backtest fills at $\text{Close}_t$, not $\text{Open}_{t+1}$
- [ ] $\Delta \text{Shares}$ reconciliation active — local `held_quantity` matches broker registry
- [ ] Capital deploy buffer (95%) applied in `calculate_position_size()`
- [ ] `holidays.NYSE` cross-check integrated alongside internal calendar
- [ ] `api_response_time` logged; Retry Queue wired for timeout events
- [ ] Telegram / alert webhook configured for CRITICAL reconciliation failures
- [ ] Backtest vs. live granularity decision documented (EOD-only or intraday bars)

---

## Appendix A: Infrastructure Patch Ledger (VTS Mock API Bypasses)

During live integration deployment, several severe sandbox anomalies within the KIS Virtual Trading Server (VTS) were programmatically intercepted and neutralized. **All patches remain active in Phase 4.**

### Incident 1: Core API Query Failure (`403 Forbidden` / Suffix Mismatch)

- **Symptom**: Connection configurations using legacy real-money environments or mismatched product suffixes threw persistent structural verification faults (`INPUT INVALID_CHECK_ACNO`, `OPSQ2000`).
- **Resolution**: Hardened authentication wrapper with `load_dotenv(override=True)`. Balance inquiry wrapped in `try_print_mock_account_balance()` — failures logged as `[INFO]` and never block the monitoring loop. Order placement proceeds under `CAPITAL_AT_RISK` from `.env`.

### Incident 2: High-Momentum Target Data Truncation (`PLTR` Candle Drop)

- **Symptom**: Querying daily historical bars for NYSE-listed tickers (`PLTR`) with standard exchange mappings (`excd: "NYS"`) returned an empty bar array (0 bars cached) due to VTS database replication lag.
- **Resolution**: Patched routing in `MARKET_META`. Forced `PLTR` candle and order execution through the NASDAQ gateway (`excd: "NAS"`, `ovrs_excg_cd: "NASD"`). Successfully caches **756** historical bars for quantitative metrics processing.

### Incident 3: Invalid Balance TR ID (`VTTT3402R`)

- **Symptom**: TR ID `VTTT3402R` returned `OPSQ0002` (invalid TR code) on VTS.
- **Resolution**: Confirmed working mock TR is **`VTRP6504R`**. Inquiry remains optional and non-blocking.

---

## Appendix B: Signal Priority & Execution Rules

Signal evaluation runs independently per ticker according to strict descending criteria priorities. **Within an active position, the 3-step sequence in Section 2 executes before priority table lookup.**

| Priority | Signal Code | Terminal Banner | Trigger Condition |
|---|---|---|---|
| **1** | `DYNAMIC_ATR_SELL` | `[KIS ORDER] SELL {TICKER} \| close-position` | Step 1: $\text{Low}_t \le \text{Trigger Floor}_{t-1}$ — **any hour if positioned** |
| **2** | `SELL` | `[KIS ORDER] SELL {TICKER} \| close-position` | Step 3: Death Cross OR RSI crossdown — **regular hours (09:30–16:00 ET) + new bar** |
| **3** | `BUY` | `[KIS ORDER] BUY {TICKER} \| market order` | Golden Cross AND RSI ≥ 50 AND Volume Gate — **regular hours + new bar** |
| **4** | `HOLD` | `[METRICS] ... Signal: HOLD` | Default; BUY blocked on liquidity fail or crossover suppression |

### Dynamic ATR Stop Formula

```
Dynamic Stop Distance   = ATR(14) × 2.0
Prior Trigger Floor     = Peak_{t-1} − Dynamic Stop Distance_{t-1}
Liquidation (Step 1)    = Low_t ≤ Prior Trigger Floor
Post-Survival (Step 2)  = Peak_t = max(Peak_{t-1}, Close_t); recalculate floor
```

---

## Appendix C: Configuration Externalization Matrix

### Zero-Leak Environment Architecture

| Source | Role |
|---|---|
| `.env` | Local secrets and overrides (never committed) |
| `.env.example` | Safe template with placeholders |
| `StrategyConfig.from_env()` | Typed strategy parameter loader |

`.gitignore` excludes: `.env`, `kis_token_cache.json`, `trading_state.json`, `project_metrics.log`, `__pycache__/`

### Full Environment Variable Reference

#### KIS Broker Credentials

| Variable | Required | Default | Description |
|---|---|---|---|
| `KIS_APP_KEY` | **Yes** | — | App Key from KIS Developers |
| `KIS_APP_SECRET` | **Yes** | — | App Secret from KIS Developers |
| `KIS_CANO` | **Yes** | — | 8-digit account number prefix |
| `KIS_ACNT_PRDT_CD` | **Yes** | — | 2-digit product suffix |

#### Portfolio, Loop & Strategy

| Variable | Required | Default | Description |
|---|---|---|---|
| `WATCHLIST` | No | `NVDA,PLTR,AAPL` | Comma-separated tickers |
| `CAPITAL_AT_RISK` | No | `10000` | USD capital base |
| `RISK_PER_TRADE` | No | `0.01` | Per-trade risk fraction |
| `LOOP_COOLDOWN_SECONDS` | No | `60` | Open-session cycle pause |
| `TICKER_SLEEP_SECONDS` | No | `1` | Inter-ticker pause |
| `MARKET_CLOSED_SLEEP_SECONDS` | No | `3600` | Holiday/weekend sleep |
| `SMA_PERIOD` | No | `10` | Trend filter period |
| `RSI_PERIOD` | No | `14` | RSI lookback |
| `ATR_PERIOD` | No | `14` | ATR lookback |
| `VOLUME_SMA_PERIOD` | No | `20` | Volume SMA period |
| `ATR_MULTIPLIER` | No | `2.0` | Trailing stop multiplier |
| `RSI_BUY_THRESHOLD` | No | `50` | BUY RSI minimum |
| `RSI_UPPER_LIMIT` | No | `70` | Crossdown threshold |
| `VOLUME_THRESHOLD` | No | `1.2` | Liquidity surge multiplier |
| `RSI_EXIT_MODE` | No | `crossdown` | `crossdown` or `threshold` |
| `USE_TRAILING_STOP` | No | `true` | Enable ATR trailing stop |
| `EXECUTION_MODE` | No | `eod` | Execution timing mode |

### Loading Chain

```
.env  →  load_dotenv(override=True)
           ├─ WATCHLIST, CAPITAL_AT_RISK, RISK_PER_TRADE, loop timing
           └─ StrategyConfig.from_env() → LiveSignalEngine + strategy.py parity
```

---

## Appendix D: Project Structure

```
Toss Trading Bot/
├── main.py                 # KIS orchestrator, MarketDataCache, holiday gate, state-gated orders, [METRICS]
├── analytics.py            # LiveSignalEngine, dual-clamp sizing, NY calendar, regular-hours gate, 3-step bar eval
├── strategy.py             # Backtrader SmaCross — look-ahead-free trailing + RSI crossdown parity
├── test_analytics.py       # Engine verification suite (3-step transition tests)
├── requirements.txt        # Python dependencies
├── .env.example            # Configuration template (copy to .env)
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

## Appendix E: Academic Regime Mapping (Watchlist Design)

The default watchlist targets three distinct volatility regimes for cross-regime empirical comparison:

| Ticker | Volatility Regime | Primary Experimental Target |
|---|---|---|
| `NVDA` | High-volatility market leader | 3-step ATR stop under elevated realized volatility |
| `PLTR` | High-momentum growth / breakout | Dual-clamp sizing + volume gate + NASD VTS proxy |
| `AAPL` | Low-volatility large-cap control | RSI crossdown stability + crossover deduplication |

Customize via `WATCHLIST` in `.env` while registering exchange routing in `MARKET_META`.

---

## License & Disclaimer

This repository is an academic quantitative infrastructure project. It connects to the **KIS Virtual Trading Server** for sandbox execution only. Past backtest performance (Phase 1–2 figures: +0.99% and +0.52% net return) does not guarantee future results. Always validate signals, `.env` configuration, holiday calendar behavior, dual-clamp sizing outputs, and account routing independently before any real-capital deployment.
