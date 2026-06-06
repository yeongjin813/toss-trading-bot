# Multi-Asset Live Signal Generator Pipeline

A production-oriented quantitative trading infrastructure for US equities. The system dynamically ingests market data, computes multi-indicator analytics per asset, enforces liquidity and volatility risk controls, and emits standardized live trading signal banners across a configurable watchlist.

| Field | Value |
|---|---|
| Current Phase | **Phase 4** — Dynamic ATR Risk Engine & Multi-Ticker Ingestion |
| Watchlist | `AAPL`, `RDW`, `JOBY` |
| Lookback Window | 3 years of daily OHLCV bars |
| Initial Capital (Backtest) | $10,000 per asset |
| Data Source | yfinance API (dynamic ingestion; local CSV cache on each run) |

---

## Project Evolution & Development Milestones (Phase 1 – 4)

### Phase 1: Static Backtesting Infrastructure (Baseline)

- **Architecture**: Single-ticker backtest on AAPL using a fixed 20-day SMA crossover over 3 years of historical daily data loaded from flat CSV files.
- **Performance**: Net return **+0.99%** — Final Portfolio Value **$10,099.42**.
- **Insight**: High vulnerability to whipsaws and capital erosion in sideways markets. A fixed lookback period without momentum or risk controls produced unreliable entry timing.

### Phase 2: Parameter Optimization & Multi-Indicator Filtering

- **Architecture**: Grid-search optimization (SMA 10–50, step 5) converged on a **10-day SMA** paired with a **14-day RSI** momentum filter (BUY when RSI >= 50; SELL when RSI > 70 or SMA death cross).
- **Performance**: Net return **+0.52%** — Final Portfolio Value **$10,052.18**.
- **Insight**: Observed **False Negative dilemma** — stacking uncorrelated indicators choked profitable entries. Reduced whipsaw frequency came at the cost of missed momentum breakouts.

### Phase 3: Production-Ready Live Signal Pipeline

- **Architecture**: Replaced static CSV dependency with automated **yfinance API ingestion** on every execution. Introduced live terminal signal banners (BUY / SELL / HOLD) driven by real-time crossover and RSI evaluation against the latest two trading sessions.
- **Capability**: Dynamic data persistence, standardized English console telemetry, and integrated Backtrader performance analyzers (Final Portfolio Value, Sharpe Ratio, Max Drawdown).

### Phase 4: Dynamic ATR Risk Engine & Multi-Ticker Ingestion *(Current)*

- **Architecture**: Expanded from single-asset to a parallel **multi-ticker watchlist** (`AAPL`, `RDW`, `JOBY`) with sequential yfinance ingestion and per-asset data persistence (`data/{ticker}_daily.csv`).
- **Liquidity Control**: Implemented a **20-day Volume SMA** filter — long entry signals are invalidated when the latest session volume falls below its 20-day average, mitigating slippage risk in mid-cap growth names such as RDW.
- **Volatility Control**: Upgraded from a static percentage trailing stop to an adaptive **Dynamic ATR Trailing Stop** — trigger floor calibrated at **`ATR(14) × 2.0`**, widening automatically during macro volatility spikes (geopolitical events, rate decisions) and tightening as volatility subsides to lock in accumulated profits.
- **Signal Priority**: `[DYNAMIC_ATR_SELL]` → `[STANDARD_SELL]` → `[BUY]` → `[HOLD]` enforced independently per ticker.

### Operational Hardening Refactor *(Latest)*

- Resolved Backtrader data feed sharing bug by guaranteeing independent object instantiation per backtest run.
- Implemented robust `REQUIRED_OHLCV_COLS` structural validation and a strict **22-bar minimum lookback gate**.
- Configured formalized annualized Sharpe Ratio analyzer calibrated for daily timeframes.
- Injected a **0.1% transaction commission** to eliminate idealized backtest optimization bias.

---

## System Architecture & Priority Order

### Multi-Asset Tracking Matrix

Each watchlist ticker is processed through an independent, parallel analytics pipeline. All assets share the same strategy parameters and risk rules but maintain isolated indicator states, position tracking, and signal output.

```
                         ┌─────────────────────────────────────────────┐
                         │           main.py — Orchestrator            │
                         │     Watchlist: [AAPL, RDW, JOBY]            │
                         └─────────────────────┬───────────────────────┘
                                               │
              ┌────────────────────────────────┼────────────────────────────────┐
              │                                │                                │
              ▼                                ▼                                ▼
     ┌─────────────────┐              ┌─────────────────┐              ┌─────────────────┐
     │  AAPL Pipeline  │              │  RDW Pipeline   │              │  JOBY Pipeline  │
     ├─────────────────┤              ├─────────────────┤              ├─────────────────┤
     │ yfinance fetch  │              │ yfinance fetch  │              │ yfinance fetch  │
     │ → aapl_daily.csv│              │ → rdw_daily.csv │              │ → joby_daily.csv│
     │ analytics.py    │              │ analytics.py    │              │ analytics.py    │
     │ Live Signal     │              │ Live Signal     │              │ Live Signal     │
     │ Backtrader BT   │              │ Backtrader BT   │              │ Backtrader BT   │
     └─────────────────┘              └─────────────────┘              └─────────────────┘
              │                                │                                │
              └────────────────────────────────┼────────────────────────────────┘
                                               ▼
                         ┌─────────────────────────────────────────────┐
                         │   Session Report + Watchlist Signal Summary │
                         └─────────────────────────────────────────────┘
```

| Layer | Module | Responsibility |
|---|---|---|
| Orchestration | `main.py` | Multi-ticker ingestion loop, per-asset session reports, watchlist signal summary |
| Indicators | `analytics.py` | SMA, RSI, ATR, Volume SMA computation; position replay; live signal evaluation |
| Strategy | `strategy.py` | Backtrader SmaCross with RSI filter, volume gate, and dynamic ATR trailing stop |
| Persistence | `data/{ticker}_daily.csv` | Normalized OHLCV cache written on every run per asset |

### Data Integrity & Backtest Calibration

| Control | Implementation |
|---|---|
| **Feed Isolation** | Each backtest run receives a fresh `load_backtrader_feed(df)` instance — baseline and risk-managed runs never share a feed object |
| **Schema Validation** | `REQUIRED_OHLCV_COLS = [Open, High, Low, Close, Volume]` — missing fields raise `ValueError` at ingestion |
| **Minimum Lookback** | `MIN_DATA_BARS = 22` enforced before indicator extraction (`max(SMA, RSI, ATR, Volume SMA) + 2`) |
| **Sharpe Analyzer** | `SharpeRatio(timeframe=Days, annualize=True)` — daily returns with annualized output |
| **Transaction Friction** | `setcommission(commission=0.001)` — 0.1% institutional commission on all backtest trades |

---

Signal evaluation runs independently for each ticker. The engine evaluates conditions in strict descending priority:

| Priority | Signal Code | Terminal Banner | Trigger Condition |
|---|---|---|---|
| **1** | `DYNAMIC_ATR_SELL` | `[LIVE SIGNAL: SELL {TICKER} - DYNAMIC ATR VOLATILITY LIQUIDATION]` | Active long position AND `today.close <= peak - (ATR(14) × 2.0)` |
| **2** | `STANDARD_SELL` | `[LIVE SIGNAL: SELL {TICKER}]` | Death cross (close crosses below SMA 10) **OR** `RSI(14) > 70` |
| **3** | `BUY` | `[LIVE SIGNAL: BUY {TICKER}]` | Golden cross (close crosses above SMA 10) **AND** `RSI(14) >= 50` **AND** Volume Liquidity Gate **PASS** |
| **4** | `HOLD` | `[LIVE SIGNAL: HOLD/HOLDING POSITION - {TICKER}]` | No higher-priority criteria met; BUY invalidated if volume < Volume SMA(20) |

#### Crossover Definitions

| Event | Condition |
|---|---|
| Golden Cross | `today.close > today.sma` **AND** `yesterday.close <= yesterday.sma` |
| Death Cross | `today.close < today.sma` **AND** `yesterday.close >= yesterday.sma` |

#### Volume Liquidity Gate

| Rule | Condition |
|---|---|
| **PASS** | `today.volume >= Volume_SMA(20)` — entry permitted |
| **FAIL** | `today.volume < Volume_SMA(20)` — BUY signal invalidated; treated as insufficient institutional liquidity |

#### Dynamic ATR Stop Formula

```
Dynamic Stop Distance = ATR(14) × 2.0
Trigger Floor Price   = Highest Price Achieved Since Entry − Dynamic Stop Distance
```

The trigger floor recalculates daily. Rising ATR widens the buffer during macro shocks; falling ATR tightens the buffer to preserve gains.

---

## Optimized Strategy Parameters

| Parameter | Value | Description |
|---|---|---|
| `sma_period` | **10** | Price trend filter (days) |
| `rsi_period` | **14** | Momentum oscillator lookback (days) |
| `rsi_buy_threshold` | **50** | Minimum RSI to confirm a BUY signal |
| `rsi_sell_threshold` | **70** | RSI overbought exit threshold |
| `atr_period` | **14** | Average True Range lookback (days) |
| `atr_multiplier` | **2.0** | Volatility buffer multiplier for trailing stop |
| `volume_sma_period` | **20** | Volume moving average for liquidity gate |
| `commission_rate` | **0.001** | Backtest transaction cost (0.1% per trade) |
| `min_data_bars` | **22** | Minimum bar count required before analytics execution |

---

## End-to-End Pipeline Flow

| Step | Action | Scope |
|---|---|---|
| 1. Ingest | Fetch 3-year daily OHLCV via yfinance; validate `REQUIRED_OHLCV_COLS` | Per asset |
| 2. Validate | Enforce `MIN_DATA_BARS` (22) before indicator computation | Per asset |
| 3. Persist | Write normalized DataFrame to `data/{ticker}_daily.csv` | Per asset |
| 4. Calculate | Compute SMA(10), RSI(14), ATR(14), Volume SMA(20) for Today and Yesterday | Per asset |
| 5. Evaluate | Apply priority-ordered signal logic with liquidity and ATR risk gates | Per asset |
| 6. Backtest | Run isolated baseline and risk-managed feeds with 0.1% commission | Per asset |
| 7. Report | Emit session telemetry, live signal banner, and backtest comparison | Per asset |
| 8. Summarize | Print consolidated watchlist signal summary | Global |

---

## Terminal Output Blueprint

### Pipeline Header

```
Multi-Asset Live Signal Generator Pipeline
Execution Timestamp : YYYY-MM-DD HH:MM:SS
Watchlist           : AAPL, RDW, JOBY
Dynamic ATR Engine  : ATR(14) x 2.0 volatility buffer
Volume Liquidity    : Volume SMA(20) entry gate
```

### Per-Ticker Session Report

Each ticker produces an independent block containing:

1. **Latest Technical Metrics** — Close, SMA, RSI, ATR, Volume, Volume SMA
2. **Volume Liquidity Filter** — Gate status (PASS / FAIL)
3. **Dynamic ATR Stop Position State** — Captured peak, trigger floor (when in position)
4. **Dynamic ATR Liquidation Telemetry** — Breach details (when triggered)
5. **Live Signal Banner** — Priority-resolved action
6. **Backtest Performance Summary** — Baseline vs Risk-Managed comparison

### Watchlist Signal Summary

```
========================================================================================
                                WATCHLIST SIGNAL SUMMARY
========================================================================================
  AAPL   -> SELL
  RDW    -> SELL
  JOBY   -> HOLD
========================================================================================
```

---

## Project Structure

```
Toss Trading Bot/
├── main.py              # Multi-ticker orchestrator: ingestion, signals, backtest
├── strategy.py          # Backtrader SmaCross with RSI, volume gate, dynamic ATR stop
├── analytics.py         # Indicator computation, position replay, signal evaluation
├── requirements.txt     # Python dependencies
├── README.md            # Project documentation
└── data/
    ├── aapl_daily.csv   # Auto-generated OHLCV cache (refreshed on each run)
    ├── rdw_daily.csv
    └── joby_daily.csv
```

---

## Dependencies

```
yfinance
pandas
numpy
matplotlib
backtrader
```

Install:

```powershell
pip install -r requirements.txt
```

---

## Usage

```powershell
python main.py
```

Each invocation sequentially ingests all watchlist tickers, evaluates live signals with full risk telemetry, prints per-asset session reports, and outputs a consolidated watchlist signal summary in a single uninterrupted run.
