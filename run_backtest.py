"""
Consolidated multi-ticker portfolio backtest runner.

P2 Portfolio Integration:
  - Single $10,000 capital pool shared across the watchlist
  - Live-parity dual-clamp position sizing (1% risk + 95% deploy cap)
  - Cross-ticker cash correlation (entries constrained by free cash)
  - Portfolio-level Return / MaxDD / Sharpe metrics

Run:
  python run_backtest.py
  python run_backtest.py --tickers NVDA,PLTR,AAPL --cash 10000
  python run_backtest.py --isolated   # legacy per-ticker mode (comparison only)
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
from portfolio_backtest import (
    PortfolioBacktestResult,
    run_portfolio_backtest,
)
from strategy import (
    DEFAULT_COMMISSION_RATE,
    create_backtest_cerebro,
    default_capital_at_risk,
)

load_dotenv(override=True)

DEFAULT_WATCHLIST = ["NVDA", "PLTR", "AAPL"]
DEFAULT_RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
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


def load_watchlist_data(
    tickers: list[str], data_dir: Path
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    ohlcv: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []

    for ticker in tickers:
        csv_path = data_dir / f"{ticker.lower()}_daily.csv"
        df = load_daily_csv(csv_path)
        if df is None or df.empty:
            skipped.append(f"{ticker} ({csv_path.name} missing or invalid)")
            continue
        ohlcv[ticker] = df

    return ohlcv, skipped


def buy_and_hold_return_pct(df: pd.DataFrame) -> float:
    start = float(df["Close"].iloc[0])
    end = float(df["Close"].iloc[-1])
    if start <= 0:
        return 0.0
    return (end / start - 1.0) * 100.0


def print_portfolio_summary(
    result: PortfolioBacktestResult,
    tickers: list[str],
    ohlcv: dict[str, pd.DataFrame],
    commission_rate: float,
    risk_per_trade: float,
) -> None:
    width = 88
    print("=" * width)
    print("PORTFOLIO BACKTEST SUMMARY (CONSOLIDATED LEDGER)".center(width))
    print("=" * width)
    print(f"Initial Capital       : ${result.initial_cash:,.2f}")
    print(f"Final Portfolio Equity: ${result.final_equity:,.2f}")
    print(f"Total Return          : {result.total_return_pct:+.2f}%")
    print(f"Portfolio MaxDD       : {result.max_drawdown_pct:.2f}%")
    print(f"Portfolio Sharpe      : {result.sharpe_ratio:.2f}")
    print(f"Total Trades          : {result.total_trades}")
    print(f"Round-Trip Wins       : {result.winning_trades}")
    print(f"Risk Per Trade        : {risk_per_trade * 100:.1f}% of total equity")
    print(f"Commission            : {commission_rate * 100:.1f}%")
    print(f"Sizing Model          : Dual-clamp (risk + 95% deploy cap on free cash)")
    print("-" * width)
    print(f"{'Ticker':<6} {'Buys':>5} {'Sells':>5} {'Open Sh':>8} {'Position':>10} {'B&H':>8}")
    print("-" * width)

    for ticker in tickers:
        if ticker not in result.per_ticker_summary:
            continue
        row = result.per_ticker_summary[ticker]
        bh = buy_and_hold_return_pct(ohlcv[ticker]) if ticker in ohlcv else 0.0
        print(
            f"{ticker:<6} "
            f"{row['buy_count']:>5} "
            f"{row['sell_count']:>5} "
            f"{row['final_shares']:>8} "
            f"{'YES' if row['in_position'] else 'NO':>10} "
            f"{bh:>+7.1f}%"
        )

    print("=" * width)

    if result.trades:
        print()
        print("TRADE LOG (chronological, shared cash ledger)".center(width))
        print("-" * width)
        print(
            f"{'Date':<12} {'Ticker':<6} {'Side':<5} {'Sh':>5} "
            f"{'Price':>9} {'Comm':>8} {'Cash':>12} {'Equity':>12} {'Signal'}"
        )
        print("-" * width)
        for trade in result.trades[-30:]:
            print(
                f"{trade.date:<12} {trade.ticker:<6} {trade.side:<5} {trade.shares:>5} "
                f"{trade.price:>9.2f} {trade.commission:>8.2f} "
                f"{trade.cash_after:>12,.2f} {trade.equity_after:>12,.2f} {trade.signal}"
            )
        if len(result.trades) > 30:
            print(f"... ({len(result.trades) - 30} earlier trades omitted)")
        print("=" * width)


def run_isolated_backtest(
    ticker: str,
    df: pd.DataFrame,
    initial_cash: float,
    commission_rate: float,
) -> dict:
    """Legacy isolated per-ticker Backtrader run (comparison baseline only)."""
    feed = bt.feeds.PandasData(dataname=df)
    cerebro = create_backtest_cerebro(
        ticker=ticker,
        initial_cash=initial_cash,
        commission_rate=commission_rate,
    )
    cerebro.addsizer(bt.sizers.PercentSizer, percents=95.0)
    cerebro.adddata(feed, name=ticker)
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Days, annualize=True, riskfreerate=0.0)
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

    return {
        "ticker": ticker,
        "final_value": end_value,
        "return_pct": (end_value / start_value - 1.0) * 100.0,
        "buy_hold_pct": buy_and_hold_return_pct(df),
        "max_drawdown_pct": float(drawdown.max.drawdown) if drawdown.max else 0.0,
        "sharpe": float(sharpe_raw) if sharpe_raw is not None else 0.0,
        "trades": total_trades,
        "win_rate_pct": (wins / total_trades * 100.0) if total_trades else 0.0,
    }


def print_isolated_table(rows: list[dict], initial_cash: float) -> None:
    width = 88
    print("=" * width)
    print("ISOLATED BACKTEST (LEGACY — NOT PORTFOLIO REALISTIC)".center(width))
    print("=" * width)
    print(
        f"{'Ticker':<6} {'Final $':>12} {'Return':>8} {'B&H':>8} "
        f"{'MaxDD':>7} {'Trades':>7} {'Win%':>6} {'Sharpe':>7}"
    )
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
    print("=" * width)
    print(f"Initial cash: ${initial_cash:,.0f} PER TICKER (independent, not consolidated)")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run consolidated portfolio backtest with dual-clamp sizing."
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
        help="Total portfolio starting capital (default: CAPITAL_AT_RISK from .env)",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory containing {ticker}_daily.csv files",
    )
    parser.add_argument(
        "--risk-per-trade",
        type=float,
        default=DEFAULT_RISK_PER_TRADE,
        help="Risk fraction of total equity per trade (default: 0.01)",
    )
    parser.add_argument(
        "--commission",
        type=float,
        default=DEFAULT_COMMISSION_RATE,
        help="Commission rate as decimal (default: 0.001)",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Run legacy isolated per-ticker Backtrader mode (comparison only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    tickers = parse_watchlist(args.tickers)
    data_dir = Path(args.data_dir)

    print("=" * 88)
    print("Toss Trading Bot - Portfolio Backtest Engine (P2)".center(88))
    print("=" * 88)
    print(f"Tickers          : {', '.join(tickers)}")
    print(f"Data dir         : {data_dir.resolve()}")
    print(f"Portfolio capital: ${args.cash:,.0f} (single consolidated pool)")
    print(f"Risk per trade   : {args.risk_per_trade * 100:.1f}% of total equity")
    print(f"Commission       : {args.commission * 100:.1f}%")
    print()

    ohlcv, skipped = load_watchlist_data(tickers, data_dir)

    if skipped:
        print("Skipped:")
        for line in skipped:
            print(f"  - {line}")
        print()

    if not ohlcv:
        print("No valid CSV data found. Place files at data/{ticker}_daily.csv")
        return 1

    loaded = [t for t in tickers if t in ohlcv]
    for ticker in loaded:
        df = ohlcv[ticker]
        cfg = StrategyConfigMapper.for_ticker(ticker)
        print(
            f"Loaded {ticker} - {len(df)} bars "
            f"({df.index[0].date()} -> {df.index[-1].date()}) | "
            f"SMA={cfg.sma_period} ATR={cfg.atr_multiplier} TrendFilter={cfg.use_trend_filter}"
        )
    print()

    if args.isolated:
        rows = [
            run_isolated_backtest(ticker, ohlcv[ticker], args.cash, args.commission)
            for ticker in loaded
        ]
        print_isolated_table(rows, args.cash)
        return 0

    result = run_portfolio_backtest(
        tickers=loaded,
        ohlcv_by_ticker=ohlcv,
        initial_cash=args.cash,
        risk_per_trade=args.risk_per_trade,
        commission_rate=args.commission,
    )

    print_portfolio_summary(
        result,
        loaded,
        ohlcv,
        commission_rate=args.commission,
        risk_per_trade=args.risk_per_trade,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
