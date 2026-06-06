# Live Signal Generator Pipeline

## Documentation Update

- Rewrote documentation to accurately reflect the dynamic live trading signal architecture.
- Added detailed sections including the end-to-end data pipeline flow, optimized parameters (SMA 10, RSI 14), and exact live breakout evaluation criteria.
- Integrated the updated project directory tree and standardized terminal output logging blueprints.
- Purged all legacy static backtesting context and localized Korean placeholders to align with production standards.

---

## 1. Project Overview

This system is a production-oriented **Live Signal Generator Pipeline** for US equities. On each execution, it dynamically ingests the latest market data, computes technical indicators, evaluates live breakout conditions, and emits a standardized BUY / SELL / HOLD signal banner. A historical backtest run is executed in parallel to report portfolio performance metrics.

| Field | Value |
|---|---|
| Target Ticker | AAPL (Apple Inc.) |
| Lookback Window | 3 years of daily OHLCV bars |
| Initial Capital (Backtest) | $10,000 |
| Data Source | yfinance API (no static CSV dependency at startup) |

---

## 2. System Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  yfinance API   │────▶│  main.py         │────▶│  data/aapl_daily.csv│
│  (3Y daily bars)│     │  fetch + persist │     │  (auto-saved cache) │
└─────────────────┘     └────────┬─────────┘     └─────────────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
             analytics.py    Live Signal     Backtrader
             SMA / RSI        Evaluation      Backtest Engine
                              (BUY/SELL/HOLD)  (Sharpe, MDD)
```

| Layer | Module | Responsibility |
|---|---|---|
| Orchestration | `main.py` | Data ingestion, signal dispatch, backtest execution, console reporting |
| Indicators | `analytics.py` | SMA, RSI calculation, latest-session metric extraction, signal evaluation |
| Strategy | `strategy.py` | Backtrader SMA crossover strategy with RSI momentum filter |
| Persistence | `data/aapl_daily.csv` | Normalized OHLCV cache written on every run |

---

## 3. End-to-End Pipeline Flow

| Step | Action | Output |
|---|---|---|
| 1. Fetch | Call yfinance for 3-year AAPL daily OHLCV data | Normalized `DataFrame` |
| 2. Persist | Write the DataFrame to `data/aapl_daily.csv` | Updated local data store |
| 3. Calculate | Compute SMA(10) and RSI(14) for Today and Yesterday | Indicator metric dict |
| 4. Evaluate | Apply crossover and RSI threshold rules | `BUY`, `SELL`, or `HOLD` |
| 5. Signal | Render the live trading banner to the terminal | Standardized banner block |
| 6. Backtest | Run the optimized strategy over full history via Backtrader | Final value, Sharpe, MDD |

---

## 4. Optimized Strategy Parameters

These parameters were selected through prior grid-search optimization (SMA periods 10–50, step 5) and are applied consistently across both the live signal engine and the backtest.

| Parameter | Value | Description |
|---|---|---|
| `sma_period` | **10** | Simple Moving Average lookback (days) |
| `rsi_period` | **14** | Relative Strength Index lookback (days) |
| `rsi_buy_threshold` | **50** | Minimum RSI required to confirm a BUY signal |
| `rsi_sell_threshold` | **70** | RSI level that triggers an overbought SELL exit |

---

## 5. Live Breakout Evaluation Criteria

The signal engine compares **Today** (most recent session) against **Yesterday** (prior session) using the following precise rules defined in `analytics.py`.

### Crossover Definitions

| Event | Condition |
|---|---|
| Golden Cross (cross above) | `today.close > today.sma` **AND** `yesterday.close <= yesterday.sma` |
| Death Cross (cross below) | `today.close < today.sma` **AND** `yesterday.close >= yesterday.sma` |

### Signal Priority (evaluated in order)

| Priority | Signal | Banner | Trigger |
|---|---|---|---|
| 1 | **SELL** | `[LIVE SIGNAL: SELL AAPL]` | Death cross **OR** `today.rsi > 70` |
| 2 | **BUY** | `[LIVE SIGNAL: BUY AAPL]` | Golden cross **AND** `today.rsi >= 50` |
| 3 | **HOLD** | `[LIVE SIGNAL: HOLD/HOLDING POSITION]` | No SELL or BUY criteria met |

---

## 6. Terminal Output Logging Blueprint

All console output is standardized in professional English. Each run produces four sequential blocks:

### Block 1 — Pipeline Header

```
Live Signal Generator Pipeline
Execution Timestamp : YYYY-MM-DD HH:MM:SS
Target Ticker       : AAPL
--------------------------------------------------------------
Data Ingestion      : N daily bars fetched and saved to ./data/aapl_daily.csv
```

### Block 2 — Latest Technical Metrics

```
Latest Technical Metrics
==============================================================
Yesterday  YYYY-MM-DD | Close: $  XXX.XX | SMA(10): $  XXX.XX | RSI(14):  XX.XX
Today      YYYY-MM-DD | Close: $  XXX.XX | SMA(10): $  XXX.XX | RSI(14):  XX.XX
==============================================================
```

### Block 3 — Live Signal Banner

```
==============================================================
                   [LIVE SIGNAL: BUY AAPL]
==============================================================
```

Valid banner values: `BUY AAPL` | `SELL AAPL` | `HOLD/HOLDING POSITION`

### Block 4 — Backtest Performance Summary

```
Backtest Performance Summary
==============================================================
Final Portfolio Value : $XX,XXX.XX
Sharpe Ratio          : X.XXXX
Max Drawdown (MDD)    : X.XX%
==============================================================
```

---

## 7. Project Structure

```
Toss Trading Bot/
├── main.py              # Pipeline orchestrator: ingestion, signals, backtest
├── strategy.py          # Backtrader SmaCross strategy (SMA + RSI filter)
├── analytics.py         # Indicator computation and live signal evaluation
├── requirements.txt     # Python dependencies
├── README.md            # Project documentation
└── data/
    └── aapl_daily.csv   # Auto-generated OHLCV cache (refreshed on each run)
```

---

## 8. Dependencies

```
yfinance
pandas
numpy
matplotlib
backtrader
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

---

## 9. Usage

```powershell
python main.py
```

Each invocation fetches fresh market data, evaluates the current session, prints the live signal banner, and reports backtest performance metrics in a single uninterrupted run.
