"""
Historical backtest runner — local CSV daily bars + strategy.py (Backtrader).

Uses the same SmaCross logic as live analytics (Phase 4 parity, set_coc=True).
No KIS API calls; reads cached OHLCV from data/{ticker}_daily.csv.

Setup:
  pip install -r requirements.txt
  Ensure CSV files exist (e.g. from a prior main.py bootstrap) or place your own.

Run:
  python run_backtest.py
  python run_backtest.py --tickers NVDA,PLTR,AAPL --cash 10000
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import backtrader as bt
import pandas as pd
from dotenv import load_dotenv

from config import StrategyConfigMapper
from strategy import (
    DEFAULT_COMMISSION_RATE,
    create_backtest_cerebro,
    default_capital_at_risk,
)

load_dotenv(override=True)

DEFAULT_WATCHLIST = ["NVDA", "PLTR", "AAPL"]
REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


def parse_watchlist(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_WATCHLIST.copy()
    tickers = [part.strip().upper() for part in raw.split(",") if part.strip()]
    return tickers or DEFAULT_WATCHLIST.copy()


def load_daily_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None

    df = pd.read_csv(path)
    if "Date" not in df.columns:
        return None

    first_cell = str(df.iloc[0, 0]).strip()
    if first_cell.lower() in {"ticker", "date"} or not first_cell[:4].isdigit():
        return None

    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").set_index("Date")
    df.columns = [str(c).capitalize() for c in df.columns]

    for column in REQUIRED_COLUMNS:
        if column not in df.columns:
            return None
        df[column] = pd.to_numeric(df[column], errors="coerce")

    return df.dropna(subset=list(REQUIRED_COLUMNS))


def buy_and_hold_return_pct(df: pd.DataFrame) -> float:
    start = float(df["Close"].iloc[0])
    end = float(df["Close"].iloc[-1])
    if start <= 0:
        return 0.0
    return (end / start - 1.0) * 100.0


def run_single_backtest(
    ticker: str,
    df: pd.DataFrame,
    initial_cash: float,
    position_pct: float,
    commission_rate: float,
) -> dict:
    feed = bt.feeds.PandasData(dataname=df)
    cerebro = create_backtest_cerebro(
        ticker=ticker,
        initial_cash=initial_cash,
        commission_rate=commission_rate,
    )
    cerebro.addsizer(bt.sizers.PercentSizer, percents=position_pct)
    cerebro.adddata(feed, name=ticker)
    cerebro.addanalyzer(
        bt.analyzers.SharpeRatio,
        _name="sharpe",
        timeframe=bt.TimeFrame.Days,
        annualize=True,
        riskfreerate=0.0,
    )
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    start_value = cerebro.broker.getvalue()
    results = cerebro.run()
    strat = results[0]
    end_value = cerebro.broker.getvalue()

    sharpe_raw = strat.analyzers.sharpe.get_analysis().get("sharperatio")
    drawdown = strat.analyzers.drawdown.get_analysis()
    trades = strat.analyzers.trades.get_analysis()

    total_trades = int(trades.total.closed) if trades.total else 0
    wins = int(trades.won.total) if trades.won else 0
    losses = int(trades.lost.total) if trades.lost else 0
    win_rate = (wins / total_trades * 100.0) if total_trades else 0.0

    return {
        "ticker": ticker,
        "bars": len(df),
        "start_date": df.index[0].strftime("%Y-%m-%d"),
        "end_date": df.index[-1].strftime("%Y-%m-%d"),
        "initial_cash": start_value,
        "final_value": end_value,
        "return_pct": (end_value / start_value - 1.0) * 100.0,
        "buy_hold_pct": buy_and_hold_return_pct(df),
        "max_drawdown_pct": float(drawdown.max.drawdown) if drawdown.max else 0.0,
        "sharpe": float(sharpe_raw) if sharpe_raw is not None else 0.0,
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
    }


def print_results_table(rows: list[dict], initial_cash: float) -> None:
    width = 88
    print("=" * width)
    print("BACKTEST SUMMARY".center(width))
    print("=" * width)
    header = (
        f"{'Ticker':<6} {'Final $':>12} {'Return':>8} {'B&H':>8} "
        f"{'MaxDD':>7} {'Trades':>7} {'Win%':>6} {'Sharpe':>7}"
    )
    print(header)
    print("-" * width)

    for row in rows:
        print(
            f"{row['ticker']:<6} "
            f"{row['final_value']:>12,.2f} "
            f"{row['return_pct']:>+7.1f}% "
            f"{row['buy_hold_pct']:>+7.1f}% "
            f"{row['max_drawdown_pct']:>6.1f}% "
            f"{row['trades']:>7} "
            f"{row['win_rate_pct']:>5.0f}% "
            f"{row['sharpe']:>7.2f}"
        )

    print("-" * width)
    if rows:
        avg_return = sum(r["return_pct"] for r in rows) / len(rows)
        avg_bh = sum(r["buy_hold_pct"] for r in rows) / len(rows)
        print(
            f"{'AVG':<6} {'':>12} {avg_return:>+7.1f}% {avg_bh:>+7.1f}%"
        )
    print("=" * width)
    print(
        f"Initial cash: ${initial_cash:,.0f} per ticker | "
        f"Commission: {DEFAULT_COMMISSION_RATE * 100:.1f}% | "
        "Execution: Close_t (set_coc=True)"
    )
    print(
        "Note: Each ticker runs independently with full capital - "
        "not a combined portfolio."
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run TrendTradingStrategy backtests on local daily CSV files."
    )
    parser.add_argument(
        "--tickers",
        default=os.getenv("WATCHLIST", ",".join(DEFAULT_WATCHLIST)),
        help="Comma-separated tickers (default: WATCHLIST from .env)",
    )
    parser.add_argument(
        "--cash",
        type=float,
        default=default_capital_at_risk(),
        help="Starting cash per ticker (default: CAPITAL_AT_RISK from .env)",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory containing {ticker}_daily.csv files",
    )
    parser.add_argument(
        "--position-pct",
        type=float,
        default=95.0,
        help="Percent of cash deployed per entry (default: 95)",
    )
    parser.add_argument(
        "--commission",
        type=float,
        default=DEFAULT_COMMISSION_RATE,
        help="Commission rate as decimal (default: 0.001)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    tickers = parse_watchlist(args.tickers)
    data_dir = Path(args.data_dir)

    print("=" * 88)
    print("Toss Trading Bot - Historical Backtest".center(88))
    print("=" * 88)
    print(f"Tickers     : {', '.join(tickers)}")
    print(f"Data dir    : {data_dir.resolve()}")
    print(f"Initial cash: ${args.cash:,.0f} (per ticker, independent runs)")
    print(f"Position    : {args.position_pct:.0f}% of cash per entry")
    print()

    results: list[dict] = []
    skipped: list[str] = []

    for ticker in tickers:
        csv_path = data_dir / f"{ticker.lower()}_daily.csv"
        df = load_daily_csv(csv_path)
        if df is None or df.empty:
            skipped.append(f"{ticker} ({csv_path.name} missing or invalid)")
            continue

        print(
            f"Running {ticker} - {len(df)} bars "
            f"({df.index[0].date()} -> {df.index[-1].date()})"
        )
        cfg = StrategyConfigMapper.for_ticker(ticker)
        print(
            f"  Regime: SMA={cfg.sma_period} RSI>={cfg.rsi_buy_threshold:.0f} "
            f"ATR={cfg.atr_multiplier:.1f} TrendFilter={cfg.use_trend_filter}"
        )
        results.append(
            run_single_backtest(
                ticker=ticker,
                df=df,
                initial_cash=args.cash,
                position_pct=args.position_pct,
                commission_rate=args.commission,
            )
        )

    print()
    if skipped:
        print("Skipped:")
        for line in skipped:
            print(f"  - {line}")
        print()

    if not results:
        print("No valid CSV data found. Place files at data/{ticker}_daily.csv")
        return 1

    print_results_table(results, args.cash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
