import os
from datetime import datetime

import backtrader as bt
import pandas as pd
import yfinance as yf

from analytics import evaluate_live_signal, extract_latest_metrics
from strategy import SmaCross

TICKER = "AAPL"
DATA_PATH = "./data/aapl_daily.csv"
INITIAL_CASH = 10000.0
OPTIMIZED_SMA_PERIOD = 10
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 50
RSI_SELL_THRESHOLD = 70
LOOKBACK_YEARS = 3


def fetch_market_data(ticker: str, years: int) -> pd.DataFrame:
    """Pull the latest daily OHLCV history from yfinance."""
    raw = yf.download(
        ticker,
        period=f"{years}y",
        progress=False,
        auto_adjust=True,
    )
    if raw.empty:
        raise RuntimeError(f"No market data returned for ticker: {ticker}")

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)

    df = raw.reset_index()
    df = df.rename(columns={"Date": "Date"})
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    return df


def persist_market_data(df: pd.DataFrame, path: str) -> None:
    """Write the normalized DataFrame to the local data store."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)


def load_backtrader_feed(df: pd.DataFrame) -> bt.feeds.PandasData:
    """Convert a normalized DataFrame into a Backtrader data feed."""
    bt_df = df.copy()
    bt_df["Date"] = pd.to_datetime(bt_df["Date"])
    bt_df.set_index("Date", inplace=True)
    bt_df.sort_index(inplace=True)

    return bt.feeds.PandasData(
        dataname=bt_df,
        datetime=None,
        open="Open",
        high="High",
        low="Low",
        close="Close",
        volume="Volume",
        openinterest=-1,
    )


def run_backtest(data_feed: bt.feeds.PandasData) -> dict:
    """Execute a single-strategy backtest with performance analyzers."""
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(INITIAL_CASH)
    cerebro.adddata(data_feed)
    cerebro.addstrategy(
        SmaCross,
        sma_period=OPTIMIZED_SMA_PERIOD,
        rsi_period=RSI_PERIOD,
        rsi_buy_threshold=RSI_BUY_THRESHOLD,
        rsi_sell_threshold=RSI_SELL_THRESHOLD,
    )
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")

    results = cerebro.run()
    strategy = results[0]

    sharpe_analysis = strategy.analyzers.sharpe.get_analysis()
    drawdown_analysis = strategy.analyzers.drawdown.get_analysis()

    sharpe_ratio = sharpe_analysis.get("sharperatio")
    max_drawdown = drawdown_analysis.get("max", {}).get("drawdown")

    return {
        "final_value": cerebro.broker.getvalue(),
        "sharpe_ratio": sharpe_ratio,
        "max_drawdown_pct": max_drawdown,
    }


def print_metrics_report(metrics: dict) -> None:
    """Print today and yesterday indicator values for auditability."""
    print("Latest Technical Metrics")
    print("=" * 62)
    for label in ("yesterday", "today"):
        row = metrics[label]
        print(
            f"{label.capitalize():<10} {row['date']} | "
            f"Close: ${row['close']:>8.2f} | "
            f"SMA({OPTIMIZED_SMA_PERIOD}): ${row['sma']:>8.2f} | "
            f"RSI({RSI_PERIOD}): {row['rsi']:>6.2f}"
        )
    print("=" * 62)


def print_live_signal_banner(ticker: str, signal: str) -> None:
    """Render the active live trading signal as a terminal banner."""
    if signal == "BUY":
        banner_text = f"LIVE SIGNAL: BUY {ticker}"
    elif signal == "SELL":
        banner_text = f"LIVE SIGNAL: SELL {ticker}"
    else:
        banner_text = "LIVE SIGNAL: HOLD/HOLDING POSITION"

    width = max(len(banner_text) + 4, 62)
    border = "=" * width
    print()
    print(border)
    print(f"[{banner_text}]".center(width))
    print(border)
    print()


def print_backtest_report(results: dict) -> None:
    """Print consolidated backtest performance metrics."""
    sharpe_display = (
        f"{results['sharpe_ratio']:.4f}"
        if results["sharpe_ratio"] is not None
        else "N/A"
    )
    mdd_display = (
        f"{results['max_drawdown_pct']:.2f}%"
        if results["max_drawdown_pct"] is not None
        else "N/A"
    )

    print("Backtest Performance Summary")
    print("=" * 62)
    print(f"Final Portfolio Value : ${results['final_value']:,.2f}")
    print(f"Sharpe Ratio          : {sharpe_display}")
    print(f"Max Drawdown (MDD)    : {mdd_display}")
    print("=" * 62)


def main() -> None:
    print("Live Signal Generator Pipeline")
    print(f"Execution Timestamp : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Target Ticker       : {TICKER}")
    print("-" * 62)

    df = fetch_market_data(TICKER, LOOKBACK_YEARS)
    persist_market_data(df, DATA_PATH)
    print(
        f"Data Ingestion      : {len(df)} daily bars fetched and saved to {DATA_PATH}"
    )

    metrics = extract_latest_metrics(df, OPTIMIZED_SMA_PERIOD, RSI_PERIOD)
    signal = evaluate_live_signal(
        metrics,
        rsi_buy_threshold=RSI_BUY_THRESHOLD,
        rsi_sell_threshold=RSI_SELL_THRESHOLD,
    )

    print_metrics_report(metrics)
    print_live_signal_banner(TICKER, signal)

    backtest_results = run_backtest(load_backtrader_feed(df))
    print_backtest_report(backtest_results)


if __name__ == "__main__":
    main()
