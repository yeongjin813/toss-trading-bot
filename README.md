# Quantitative Trading Infrastructure & Rule-Based Strategy Backtesting System

## 1. Project Overview

- **Objective**: Design and implement an automated financial data pipeline and a systematic backtesting framework using Python to evaluate time-series trading strategies.
- **Problem Statement**: Standardizing raw market data ingestion to mitigate whipsaw losses in range-bound markets through quantitative parameter optimization.

## 2. System Architecture & Tech Stack

- **Data Ingestion**: `yfinance` for downloading historical US equity time-series data (AAPL, MSFT).
- **Data Engineering**: `Pandas` and `NumPy` for data cleaning `dropna`), index alignment `Date`), and modular pipeline design.
- **Backtesting Engine**: `Backtrader` framework utilized to simulate realistic trading environments (initial cash: $10,000, execution logic).
- **Analytics**: Custom technical indicators including Simple Moving Average (SMA) and Relative Strength Index (RSI).

## 3. Quantitative Strategy & Optimization

### Strategy V1: Simple SMA Cross

- **Logic**: All-in BUY when price crosses above 20-day SMA; Full SELL when price crosses below 20-day SMA.
- **Performance**: Final Portfolio Value: $10,099.42 (Return: +0.99%)

### Strategy V2: SMA Cross with RSI Filter

- **Logic**: Added RSI (14) as a secondary filter (BUY only when RSI >= 50; SELL when RSI > 70 or SMA dead-cross).
- **Performance**: Final Portfolio Value: $10,052.18 (Return: +0.52%)
- **Data Insight**: The RSI filter reduced trading frequency, preventing whipsaws but introducing false negatives during major momentum shifts.

### Strategy V3: Grid Search Parameter Optimization

- **Logic**: Systematically tested SMA periods from 10 to 50 days (step=5) to maximize Sharpe Ratio and net portfolio value.
- **Result**: [3일차 연산이 끝나면 여기에 최적의 숫자와 결과를 기록할 것]