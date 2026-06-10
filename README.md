# Multi-Asset Live Signal Generator & Quantitative Trading Pipeline

An automated quantitative trading infrastructure and empirical data collection pipeline built on top of the Korea Investment & Securities (KIS) Overseas Market API. The system dynamically ingests live market sequences, computes multi-indicator analytics per asset, enforces liquidity/volatility risk controls, and emits standardized execution telemetry while gracefully bypassing mock-brokerage infrastructure constraints.

| Field | Value |
|---|---|
| **Current Phase** | **Phase 4** — KIS Production Sync & Multi-Ticker Telemetry Ingestion |
| **Watchlist** | `NVDA`, `PLTR`, `AAPL` (Targeted Across Distinct Volatility Regimes) |
| **Broker Infrastructure** | Korea Investment & Securities (KIS) Developers API (Virtual Trading Server) |
| **Target Unified Account** | `CANO: 50191906` \| `ACNT_PRDT_CD: 01` (Unified Portfolio Suffix) |
| **Initial Capital Base** | $10,000 equivalent per asset registry |
| **Data Architecture** | 60-Second Loop Polling with Automated Stream Redirection Support |

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

### Phase 4: Dynamic ATR Risk Engine & KIS API Integration *(Current)*

- **Architecture**: Expanded from single-asset to a parallel multi-ticker watchlist (`NVDA`, `PLTR`, `AAPL`) executing live routines through the KIS sandbox server (`openapivts.koreainvestment.com:29443`).
- **Liquidity Control**: Implemented a **20-day Volume SMA** filter — long entry signals are invalidated when the latest session volume falls below **120%** of its 20-day average (`volume_threshold = 1.2`), mitigating slippage risk in high-momentum growth names such as `PLTR`.
- **Volatility Control**: Upgraded from a static percentage trailing stop to an adaptive **Dynamic ATR Trailing Stop** — trigger floor calibrated at **`ATR(14) × 2.0`**, widening automatically during macro volatility spikes and tightening as volatility subsides to lock in accumulated profits.
- **Operational Hardening**: Independent Backtrader feed isolation per run, `REQUIRED_OHLCV_COLS` schema validation, 22-bar minimum lookback gate, annualized Sharpe analyzer, and **0.1%** backtest commission calibration.

---

## 2. System Architecture & Core Subsystems

### Multi-Asset Tracking Matrix

Each watchlist ticker is processed through an independent analytics pipeline, sharing parameter rules but maintaining isolated states.

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

| Layer | Module | Responsibility |
|---|---|---|
| Orchestration | `main.py` | KIS auth, 60s watchlist loop, order routing, `[METRICS]` telemetry, state persistence |
| Indicators | `analytics.py` | Wilder RSI/ATR, SMA, Volume SMA; `LiveSignalEngine` bar evaluation |
| Strategy | `strategy.py` | Backtrader `SmaCross` with RSI filter, volume gate, dynamic ATR trailing stop |
| Persistence | `data/{ticker}_daily.csv`, `trading_state.json` | OHLCV cache and per-ticker position state |

### Strategic Parameter Telemetry Disconnect

To capture cross-regime statistical divergence for empirical visualization, the pipeline implements an intentional telemetry disconnect:

- **Execution Signal Engine**: Evaluates entries using a short-term trend index ($\text{SMA}_{10}$) via `STRATEGY_CONFIG` for higher breakout sensitivity.
- **Telemetry Logging Layer**: Independent extraction of a medium-term structural baseline ($\text{SMA}_{20}$) mapped onto the streaming telemetry log for external regression/confusion-matrix modeling (`ANALYTICS_SMA_PERIOD = 20` in `main.py`).

### Continuous Monitoring Loop

```
validate_environment() → KIS token → try_print_mock_account_balance() [non-blocking]
→ log_configured_capital_model() → while True:
      run_watchlist_cycle() → sleep(LOOP_COOLDOWN_SECONDS)
```

| Constant | Value | Role |
|---|---|---|
| `LOOP_COOLDOWN_SECONDS` | 60 | Inter-cycle pause |
| `TICKER_SLEEP_SECONDS` | 1 | Pause between tickers within a cycle |
| `CAPITAL_AT_RISK` | $10,000 | Manual position-sizing base (not gated on live balance) |
| `RISK_PER_TRADE` | 1% | Per-trade risk budget for share sizing |

---

## 3. Architectural Defense Ledger (Code Modification Post-Mortem)

During development, external code reviews suggested injecting generic event-driven tracking layers (e.g., Backtrader-style `notify_order()` state parameters or strict asynchronous order locks).

After rigorous architectural evaluation, **these modifications were rejected, and the existing framework was maintained based on the following engineering rationales:**

1. **Decoupled Metric Extraction Integrity**: The system's primary academic objective is the continuous collection of pure, continuous time-series data via the `[METRICS]` channel. Injecting state-gated blocking parameters inside the indicator processing loops introduces the risk of telemetry data gaps if an order is held in a prolonged pending state by the broker.
2. **Asymmetrical Error Resiliency**: The infrastructure is designed to intentionally ignore the KIS Mock Server's unstable balance inquiry failures caused by KRW/USD cross-currency account parsing and CANO/suffix mismatches. Forcing a synchronous state machine check would create deadlocks when interacting with an inherently delayed mock API framework. Order placement proceeds under the assumption that capital is manually configured via `STRATEGY_CONFIG`.
3. **Deterministic Local State Handling**: The existing loop architecture utilizes native exception handlers and records transactional vectors directly to `./trading_state.json` via a graceful `KeyboardInterrupt` hook. This provides sufficient state safety without over-engineering the codebase prior to production data verification.

---

## 4. Infrastructure Patch Ledger (VTS Mock API Bypasses)

During live integration deployment, several severe sandbox anomalies within the KIS Virtual Trading Server (VTS) were programmatically intercepted and neutralized:

### Incident 1: Core API Query Failure (`403 Forbidden` / Suffix Mismatch)

- **Symptom**: Connection configurations using legacy real-money environments (`44708344`) or mismatched product suffixes (`02`) threw persistent structural verification faults (`INPUT INVALID_CHECK_ACNO`, `OPSQ2000`).
- **Resolution**: Hardened the authentication wrapper to dynamically prioritize environment overrides (`load_dotenv(override=True)`). Remapped the ecosystem to anchor on the unified active mock portfolio `50191906-01` with freshly generated VTS API keys. Balance inquiry is wrapped in `try_print_mock_account_balance()` — failures are logged as `[INFO]` and never block the monitoring loop.

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

---

## 5. Signal Priority & Execution Rules

Signal evaluation runs independently per ticker according to strict descending criteria priorities:

| Priority | Signal Code | Terminal Banner / Log Ingestion Template | Trigger Condition |
|---|---|---|---|
| **1** | `DYNAMIC_ATR_SELL` | `[LIVE SIGNAL: SELL {TICKER} - DYNAMIC ATR VOLATILITY LIQUIDATION]` | Position active AND $\text{Low}_t \le \text{Peak} - (\text{ATR}_{14} \times 2.0)$ |
| **2** | `SELL` | `[LIVE SIGNAL: SELL {TICKER}]` | Death Cross ($\text{Close}_t < \text{SMA}_{10}$ while $\text{Close}_{t-1} \ge \text{SMA}_{10}$) OR RSI crossdown exit ($\text{RSI}_{t-1} \ge 70$ and $\text{RSI}_t < 70$) |
| **3** | `BUY` | `[LIVE SIGNAL: BUY {TICKER}]` | Golden Cross ($\text{Close}_t > \text{SMA}_{10}$ while $\text{Close}_{t-1} \le \text{SMA}_{10}$) AND $\text{RSI}_{14} \ge 50$ AND Volume Gate **PASS** |
| **4** | `HOLD` | `[LIVE SIGNAL: HOLD/HOLDING POSITION - {TICKER}]` | Default state; BUY invalidated if Volume Gate **FAIL** |

- **Volume Liquidity Gate PASS**: $\text{Volume}_t > \text{Volume\_SMA}_{20} \times 1.2$ → Entry permitted.
- **Volume Liquidity Gate FAIL**: $\text{Volume}_t \le \text{Volume\_SMA}_{20} \times 1.2$ → Signal downgraded to `HOLD`.

#### Dynamic ATR Stop Formula

```
Dynamic Stop Distance = ATR(14) × 2.0
Trigger Floor Price   = Highest Close Since Entry − Dynamic Stop Distance
Liquidation Trigger   = Session Low ≤ Trigger Floor
```

---

## 6. Optimized Strategy Parameters

| Parameter | Value | Description |
|---|---|---|
| `sma_period` | **10** | Price trend filter (days) — execution signal engine |
| `ANALYTICS_SMA_PERIOD` | **20** | Structural baseline for `[METRICS]` telemetry only |
| `rsi_period` | **14** | Momentum oscillator lookback (days); Wilder smoothing |
| `rsi_buy_threshold` | **50** | Minimum RSI to confirm a BUY signal |
| `rsi_upper_limit` | **70** | RSI overbought exit threshold (crossdown mode) |
| `rsi_exit_mode` | **crossdown** | Exit when RSI crosses below 70 from above |
| `atr_period` | **14** | Average True Range lookback (days); Wilder smoothing |
| `atr_multiplier` | **2.0** | Volatility buffer multiplier for trailing stop |
| `volume_sma_period` | **20** | Volume moving average for liquidity gate |
| `volume_threshold` | **1.2** | Volume must exceed 120% of 20-day average for BUY |
| `commission_rate` | **0.001** | Backtest transaction cost (0.1% per trade) |
| `min_data_bars` | **22** | Minimum bar count required before analytics execution |
| `CAPITAL_AT_RISK` | **10000** | Manual capital base for live position sizing (USD) |
| `RISK_PER_TRADE` | **0.01** | Fraction of capital risked per trade (1%) |

---

## 7. Operational Guidelines

### Clean Environment Variable Initialization (`.env`)

Ensure your localized root deployment contains the synchronized variables. **Never commit `.env` to version control** — it is listed in `.gitignore`. Copy from `.env.example` and substitute your own KIS Developers portal credentials:

```ini
KIS_APP_KEY=your_app_key_here
KIS_APP_SECRET=your_app_secret_here
KIS_CANO=50191906
KIS_ACNT_PRDT_CD=01
```

| Variable | Description |
|---|---|
| `KIS_APP_KEY` | App Key from KIS Developers (VTS / mock trading) |
| `KIS_APP_SECRET` | App Secret from KIS Developers |
| `KIS_CANO` | 8-digit account number prefix |
| `KIS_ACNT_PRDT_CD` | 2-digit product suffix (`01` unified portfolio) |

`load_dotenv(override=True)` ensures `.env` values take precedence over stale shell exports when suffix or CANO changes between sessions.

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

1. Environment validation (`validate_environment()`)
2. KIS OAuth token issuance (cached in `kis_token_cache.json`)
3. Optional balance probe (`try_print_mock_account_balance`) — failures ignored
4. Capital model banner (`log_configured_capital_model`) — confirms `$10,000` manual sizing
5. Infinite watchlist loop with per-ticker `[METRICS]` emission

### Empirical Log Redirection

Redirect the full telemetry stream for offline visualization (confusion matrices, drawdown curves, regime comparison):

```powershell
python main.py *> project_metrics.log
```

Sample `[METRICS]` line (pipe-delimited for parsing):

```
[METRICS] NVDA | 2026-06-05 14:30:01 | Close=142.50 | SMA20=138.20 | RSI=58.32 | ATR_Wilder=4.2150 | Volume=45,230,000/38,100,000 (1.187x)
```

### Verification Suite

```powershell
python test_analytics.py
```

Runs mock-bar unit tests against `LiveSignalEngine` (RSI/ATR computation, bar transitions, signal priority).

### Graceful Shutdown

Press `Ctrl+C` during the monitoring loop. The handler persists the latest multi-ticker state to `trading_state.json` before exit.

---

## 8. KIS API Reference (Live Production Paths)

| Operation | TR ID (Mock) | HTTP Path |
|---|---|---|
| OAuth Token | — | `POST /oauth2/tokenP` |
| US Daily OHLCV | `HHDFS76240000` | `GET /uapi/overseas-price/v1/quotations/dailyprice` |
| US Market Buy | `VTTT1002U` | `POST /uapi/overseas-stock/v1/trading/order` |
| US Market Sell | `VTTT1006U` | `POST /uapi/overseas-stock/v1/trading/order` |
| Present Balance *(optional)* | `VTRP6504R` | `GET /uapi/overseas-stock/v1/trading/inquire-present-balance` |
| Order Hashkey | — | `POST /uapi/hashkey` |

**Base URL (VTS Mock):** `https://openapivts.koreainvestment.com:29443`

---

## 9. Project Structure

```
Toss Trading Bot/
├── main.py                 # KIS orchestrator, watchlist loop, order routing, [METRICS] telemetry
├── analytics.py            # LiveSignalEngine, Wilder RSI/ATR, signal evaluation
├── strategy.py             # Backtrader SmaCross backtest strategy
├── test_analytics.py       # Engine verification suite
├── requirements.txt        # Python dependencies
├── .env.example            # Credential template (copy to .env)
├── .gitignore              # Excludes .env, kis_token_cache.json, __pycache__
├── trading_state.json      # Multi-ticker position state (auto-generated)
├── kis_token_cache.json    # OAuth token cache (auto-generated)
├── project_metrics.log     # Example redirected telemetry output
└── data/
    ├── nvda_daily.csv      # KIS OHLCV cache (refreshed each cycle)
    ├── pltr_daily.csv
    └── aapl_daily.csv
```

---

## 10. Academic Regime Mapping (Watchlist Design)

| Ticker | Volatility Regime | Primary Experimental Target |
|---|---|---|
| `NVDA` | High-volatility market leader | Dynamic ATR Trailing Stop stress test |
| `PLTR` | High-momentum growth / breakout | Volume Liquidity Gate validation |
| `AAPL` | Low-volatility large-cap control | Baseline comparative analysis |

This three-asset design enables cross-regime empirical comparison: ATR stop behavior under elevated realized volatility (`NVDA`), liquidity-filter efficacy on momentum names (`PLTR`), and signal stability on a mature benchmark (`AAPL`).

---

## License & Disclaimer

This repository is an academic quantitative infrastructure project. It connects to the **KIS Virtual Trading Server** for sandbox execution only. Past backtest performance (Phase 1–2 figures) does not guarantee future results. Always validate signals and account configuration independently before any real-capital deployment.
