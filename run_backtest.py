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
  python run_backtest.py --walk-forward
  python run_backtest.py --isolated   # legacy per-ticker mode (comparison only)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import backtrader as bt
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from config import StrategyConfigMapper
from market_registry import BENCHMARK_TICKER, DEFAULT_WATCHLIST, SECONDARY_BENCHMARK_TICKER, parse_watchlist
from momentum_ranker import MomentumRankSettings
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

DEFAULT_RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")
DEFAULT_RANDOM_BARS = 252
RANDOM_START_PRICES: dict[str, float] = {
    "NVDA": 200.0,
    "PLTR": 130.0,
    "AAPL": 290.0,
    "SPY": 450.0,
}
RANDOM_DAILY_VOL: dict[str, float] = {
    "NVDA": 0.028,
    "PLTR": 0.035,
    "AAPL": 0.018,
    "SPY": 0.012,
}
WALK_FORWARD_WINDOWS: list[tuple[str, str, str]] = [
    ("2018-2020", "2018-01-01", "2020-12-31"),
    ("2020-2022", "2020-01-01", "2022-12-31"),
    ("2022-2024", "2022-01-01", "2024-12-31"),
    ("2024-2026", "2024-01-01", "2026-12-31"),
]
YFINANCE_WARMUP_START = "2017-01-01"


def resolve_momentum_settings(args: argparse.Namespace) -> MomentumRankSettings:
    settings = MomentumRankSettings.from_env()
    enabled = settings.enabled and not args.no_momentum_rank
    top_n = args.momentum_top_n if args.momentum_top_n is not None else settings.top_n
    return MomentumRankSettings(
        enabled=enabled,
        top_n=top_n,
        rebalance_weekday=settings.rebalance_weekday,
        weight_3m=settings.weight_3m,
        weight_6m=settings.weight_6m,
        weight_12m=settings.weight_12m,
        weight_volume=settings.weight_volume,
        require_above_sma50=settings.require_above_sma50,
        require_above_sma200=settings.require_above_sma200,
        min_bars=settings.min_bars,
    )


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


def normalize_yfinance_frame(raw: pd.DataFrame) -> pd.DataFrame:
    frame = raw.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [str(level[0]).capitalize() for level in frame.columns]
    else:
        frame.columns = [str(c).capitalize() for c in frame.columns]

    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)

    frame = frame.sort_index()
    for column in REQUIRED_COLUMNS:
        if column not in frame.columns:
            raise ValueError(f"Missing column {column} in yfinance download.")
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    return frame.dropna(subset=list(REQUIRED_COLUMNS))


def fetch_yfinance_ohlcv(
    ticker: str,
    start: str,
    end: str | None = None,
) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(
        ticker,
        start=start,
        end=end,
        progress=False,
        auto_adjust=True,
    )
    if raw is None or raw.empty:
        raise ValueError(f"No yfinance data returned for {ticker} ({start} -> {end})")
    return normalize_yfinance_frame(raw)


def slice_ohlcv_window(
    df: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    window = df.loc[(df.index >= start_ts) & (df.index <= end_ts)].copy()
    return window


def generate_random_ohlcv(
    ticker: str,
    bars: int = DEFAULT_RANDOM_BARS,
    seed: int | None = None,
) -> pd.DataFrame:
    """Synthetic 1-year daily OHLCV via geometric random walk (smoke-test data)."""
    rng = np.random.default_rng(seed)
    start_price = RANDOM_START_PRICES.get(ticker, 100.0)
    daily_vol = RANDOM_DAILY_VOL.get(ticker, 0.025)
    base_volume = {"NVDA": 150_000_000, "PLTR": 40_000_000, "AAPL": 50_000_000}.get(
        ticker, 20_000_000
    )

    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=bars)
    returns = rng.normal(loc=0.0002, scale=daily_vol, size=bars)
    closes = start_price * np.cumprod(1.0 + returns)

    records: list[dict[str, float]] = []
    for idx, close in enumerate(closes):
        intraday_noise = abs(rng.normal(0.0, daily_vol * 0.35))
        open_price = close * (1.0 + rng.normal(0.0, daily_vol * 0.15))
        high = max(open_price, close) * (1.0 + intraday_noise)
        low = min(open_price, close) * (1.0 - intraday_noise)
        volume = max(
            1_000_000.0,
            base_volume * float(np.exp(rng.normal(0.0, 0.45))),
        )
        records.append(
            {
                "Open": round(float(open_price), 4),
                "High": round(float(high), 4),
                "Low": round(float(low), 4),
                "Close": round(float(close), 4),
                "Volume": round(volume, 0),
            }
        )

    return pd.DataFrame(records, index=dates)


def load_random_watchlist_data(
    tickers: list[str],
    bars: int,
    seed: int,
) -> dict[str, pd.DataFrame]:
    return {
        ticker: generate_random_ohlcv(
            ticker,
            bars=bars,
            seed=seed + sum(ord(char) for char in ticker),
        )
        for ticker in tickers
    }


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


def load_yfinance_watchlist_data(
    tickers: list[str],
    start: str,
    end: str | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    ohlcv: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []

    for ticker in tickers:
        try:
            ohlcv[ticker] = fetch_yfinance_ohlcv(ticker, start=start, end=end)
        except Exception as exc:
            skipped.append(f"{ticker} (yfinance error: {exc})")

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
    *,
    title_suffix: str = "",
) -> None:
    width = 88
    title = "PORTFOLIO BACKTEST SUMMARY (CONSOLIDATED LEDGER)"
    if title_suffix:
        title = f"{title} - {title_suffix}"
    print("=" * width)
    print(title.center(width))
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


def print_walk_forward_table(rows: list[dict[str, Any]]) -> None:
    width = 88
    print("=" * width)
    print("WALK-FORWARD VALIDATION (PORTFOLIO)".center(width))
    print("=" * width)
    print(
        f"{'Window':<12} {'Return':>8} {'MaxDD':>7} {'Sharpe':>7} "
        f"{'Trades':>7} {'Wins':>5} {'Final $':>12}"
    )
    print("-" * width)
    for row in rows:
        print(
            f"{row['label']:<12} "
            f"{row['return_pct']:>+7.2f}% "
            f"{row['max_drawdown_pct']:>6.2f}% "
            f"{row['sharpe']:>7.2f} "
            f"{row['trades']:>7} "
            f"{row['wins']:>5} "
            f"{row['final_equity']:>12,.2f}"
        )
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
    print("ISOLATED BACKTEST (LEGACY - NOT PORTFOLIO REALISTIC)".center(width))
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
        "--start",
        default=None,
        help="Optional inclusive start date (YYYY-MM-DD) for CSV backtest window",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Optional inclusive end date (YYYY-MM-DD) for CSV backtest window",
    )
    parser.add_argument(
        "--yfinance",
        action="store_true",
        help="Download OHLCV from yfinance instead of local CSV files",
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Run 2018-2020 / 2020-2022 / 2022-2024 / 2024-2026 validation windows",
    )
    parser.add_argument(
        "--no-spy-filter",
        action="store_true",
        help="Disable SPY > 200MA entry gate (default: enabled via USE_SPY_MARKET_FILTER)",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Run legacy isolated per-ticker Backtrader mode (comparison only)",
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="Use synthetic random OHLCV instead of CSV files (smoke test)",
    )
    parser.add_argument(
        "--random-bars",
        type=int,
        default=DEFAULT_RANDOM_BARS,
        help="Trading days for --random data (default: 252)",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=42,
        help="RNG seed for --random data (default: 42)",
    )
    parser.add_argument(
        "--no-momentum-rank",
        action="store_true",
        help="Disable weekly momentum Top-N entry filter (default: enabled via MOMENTUM_RANK_ENABLED)",
    )
    parser.add_argument(
        "--momentum-top-n",
        type=int,
        default=None,
        help="Override MOMENTUM_TOP_N for this backtest run",
    )
    return parser


def run_walk_forward_validation(args: argparse.Namespace, tickers: list[str]) -> int:
    use_spy_filter = not args.no_spy_filter and StrategyConfigMapper.use_spy_market_filter()
    momentum_settings = resolve_momentum_settings(args)
    print("Fetching extended yfinance history for walk-forward validation...")
    print(f"Tickers: {', '.join(tickers)}")
    print(f"SPY market filter: {'ON' if use_spy_filter else 'OFF'}")
    print(
        f"Momentum rank    : "
        f"{'ON (Top ' + str(momentum_settings.top_n) + ')' if momentum_settings.enabled else 'OFF'}"
    )
    print()

    full_ohlcv, skipped = load_yfinance_watchlist_data(
        tickers,
        start=YFINANCE_WARMUP_START,
    )
    if skipped:
        print("Skipped tickers:")
        for line in skipped:
            print(f"  - {line}")
        print()

    loaded = [t for t in tickers if t in full_ohlcv]
    if len(loaded) < 2:
        print("Need at least 2 tickers with valid yfinance data for portfolio backtest.")
        return 1

    spy_df: pd.DataFrame | None = None
    qqq_df: pd.DataFrame | None = None
    if use_spy_filter:
        try:
            spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
            if StrategyConfigMapper.use_qqq_regime_filter():
                qqq_df = fetch_yfinance_ohlcv(
                    SECONDARY_BENCHMARK_TICKER,
                    start=YFINANCE_WARMUP_START,
                )
        except Exception as exc:
            print(f"[WARN] Could not load {BENCHMARK_TICKER}: {exc} - filter disabled.")
            use_spy_filter = False
            qqq_df = None

    summary_rows: list[dict[str, Any]] = []
    for label, start, end in WALK_FORWARD_WINDOWS:
        window_ohlcv: dict[str, pd.DataFrame] = {}
        for ticker in loaded:
            sliced = slice_ohlcv_window(full_ohlcv[ticker], start, end)
            if len(sliced) >= 22:
                window_ohlcv[ticker] = sliced

        if len(window_ohlcv) < 2:
            print(f"[SKIP] {label}: insufficient ticker coverage in window.")
            continue

        result = run_portfolio_backtest(
            tickers=list(window_ohlcv.keys()),
            ohlcv_by_ticker=window_ohlcv,
            initial_cash=args.cash,
            risk_per_trade=args.risk_per_trade,
            commission_rate=args.commission,
            use_spy_market_filter=use_spy_filter,
            spy_df=spy_df,
            qqq_df=qqq_df,
            momentum_settings=momentum_settings,
        )
        summary_rows.append(
            {
                "label": label,
                "return_pct": result.total_return_pct,
                "max_drawdown_pct": result.max_drawdown_pct,
                "sharpe": result.sharpe_ratio,
                "trades": result.total_trades,
                "wins": result.winning_trades,
                "final_equity": result.final_equity,
            }
        )
        print_portfolio_summary(
            result,
            list(window_ohlcv.keys()),
            window_ohlcv,
            commission_rate=args.commission,
            risk_per_trade=args.risk_per_trade,
            title_suffix=label,
        )
        print()

    if summary_rows:
        print_walk_forward_table(summary_rows)
    else:
        print("No walk-forward windows produced results.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    tickers = parse_watchlist(args.tickers)
    data_dir = Path(args.data_dir)
    use_spy_filter = not args.no_spy_filter and StrategyConfigMapper.use_spy_market_filter()
    momentum_settings = resolve_momentum_settings(args)

    if args.walk_forward:
        return run_walk_forward_validation(args, tickers)

    print("=" * 88)
    print("Toss Trading Bot - Portfolio Backtest Engine (P2)".center(88))
    print("=" * 88)
    print(f"Tickers          : {', '.join(tickers)}")
    if args.random:
        print(f"Data source      : synthetic random ({args.random_bars} bars, seed={args.random_seed})")
    elif args.yfinance:
        print(f"Data source      : yfinance (start={args.start or YFINANCE_WARMUP_START})")
    else:
        print(f"Data dir         : {data_dir.resolve()}")
    print(f"Portfolio capital: ${args.cash:,.0f} (single consolidated pool)")
    print(f"Risk per trade   : {args.risk_per_trade * 100:.1f}% of total equity")
    print(f"Commission       : {args.commission * 100:.1f}%")
    print(
        f"SPY market filter: "
        f"{'ON (BUY only when SPY > 200MA)' if use_spy_filter else 'OFF'}"
    )
    print(
        f"Momentum rank    : "
        f"{'ON (Top ' + str(momentum_settings.top_n) + ', Friday rebalance)' if momentum_settings.enabled else 'OFF'}"
    )
    print()

    if args.random:
        ohlcv = load_random_watchlist_data(tickers, args.random_bars, args.random_seed)
        skipped: list[str] = []
        spy_df = None
    elif args.yfinance:
        ohlcv, skipped = load_yfinance_watchlist_data(
            tickers,
            start=args.start or YFINANCE_WARMUP_START,
            end=args.end,
        )
    else:
        ohlcv, skipped = load_watchlist_data(tickers, data_dir)

        if skipped:
            print("Skipped:")
            for line in skipped:
                print(f"  - {line}")
            print()

        if not ohlcv:
            print("No valid CSV data found. Place files at data/{ticker}_daily.csv")
            print("Tip: use --yfinance or --walk-forward for extended history.")
            return 1

    if args.start or args.end:
        start = args.start or "1900-01-01"
        end = args.end or pd.Timestamp.today().strftime("%Y-%m-%d")
        ohlcv = {
            ticker: slice_ohlcv_window(df, start, end)
            for ticker, df in ohlcv.items()
            if not slice_ohlcv_window(df, start, end).empty
        }

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

    spy_df: pd.DataFrame | None = None
    qqq_df: pd.DataFrame | None = None
    if use_spy_filter and not args.random:
        try:
            if args.yfinance or args.walk_forward:
                spy_df = fetch_yfinance_ohlcv(
                    BENCHMARK_TICKER,
                    start=args.start or YFINANCE_WARMUP_START,
                    end=args.end,
                )
            else:
                spy_path = data_dir / f"{BENCHMARK_TICKER.lower()}_daily.csv"
                spy_df = load_daily_csv(spy_path)
                if spy_df is None:
                    spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
            if StrategyConfigMapper.use_qqq_regime_filter():
                qqq_df = fetch_yfinance_ohlcv(
                    SECONDARY_BENCHMARK_TICKER,
                    start=args.start or YFINANCE_WARMUP_START,
                    end=args.end,
                )
        except Exception as exc:
            print(f"[WARN] SPY benchmark unavailable ({exc}) - market filter disabled.")
            use_spy_filter = False
            qqq_df = None

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
        use_spy_market_filter=use_spy_filter,
        spy_df=spy_df,
        qqq_df=qqq_df,
        momentum_settings=momentum_settings,
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
