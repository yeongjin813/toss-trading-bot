import os
from datetime import datetime

import backtrader as bt
import pandas as pd
import yfinance as yf

from analytics import derive_position_state, evaluate_live_signal, extract_latest_metrics
from strategy import SmaCross

WATCHLIST = ["AAPL", "RDW", "JOBY"]
DATA_DIR = "./data"
INITIAL_CASH = 10000.0
OPTIMIZED_SMA_PERIOD = 10
RSI_PERIOD = 14
ATR_PERIOD = 14
ATR_MULTIPLIER = 2.0
VOLUME_SMA_PERIOD = 20
RSI_BUY_THRESHOLD = 50
RSI_SELL_THRESHOLD = 70
LOOKBACK_YEARS = 3
MIN_DATA_BARS = (
    max(OPTIMIZED_SMA_PERIOD, RSI_PERIOD, ATR_PERIOD, VOLUME_SMA_PERIOD) + 2
)
REQUIRED_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]
COMMISSION_RATE = 0.001


def data_path_for_ticker(ticker: str) -> str:
    return os.path.join(DATA_DIR, f"{ticker.lower()}_daily.csv")


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

    missing_cols = [col for col in REQUIRED_OHLCV_COLS if col not in df.columns]
    if missing_cols:
        raise ValueError(
            f"Missing required columns for {ticker}: {', '.join(missing_cols)}"
        )

    df = df[["Date", *REQUIRED_OHLCV_COLS]].copy()
    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    return df


def validate_dataframe_length(df: pd.DataFrame, ticker: str) -> None:
    """Ensure sufficient bar count before indicator extraction and replay."""
    if len(df) < MIN_DATA_BARS:
        raise ValueError(
            f"Insufficient data for {ticker}: {len(df)} bars "
            f"(minimum required: {MIN_DATA_BARS})"
        )


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


def run_backtest(
    data_feed: bt.feeds.PandasData,
    enable_dynamic_atr_stop: bool = True,
    enable_volume_filter: bool = True,
) -> dict:
    """Execute backtest with optional dynamic ATR stop and volume filter."""
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(INITIAL_CASH)
    cerebro.broker.setcommission(commission=COMMISSION_RATE)
    cerebro.adddata(data_feed)
    cerebro.addstrategy(
        SmaCross,
        sma_period=OPTIMIZED_SMA_PERIOD,
        rsi_period=RSI_PERIOD,
        rsi_buy_threshold=RSI_BUY_THRESHOLD,
        rsi_sell_threshold=RSI_SELL_THRESHOLD,
        atr_period=ATR_PERIOD,
        atr_multiplier=ATR_MULTIPLIER,
        volume_sma_period=VOLUME_SMA_PERIOD,
        enable_dynamic_atr_stop=enable_dynamic_atr_stop,
        enable_volume_filter=enable_volume_filter,
    )
    cerebro.addanalyzer(
        bt.analyzers.SharpeRatio,
        _name="sharpe",
        timeframe=bt.TimeFrame.Days,
        annualize=True,
    )
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")

    results = cerebro.run()
    strategy = results[0]

    sharpe_analysis = strategy.analyzers.sharpe.get_analysis()
    drawdown_analysis = strategy.analyzers.drawdown.get_analysis()

    return {
        "final_value": cerebro.broker.getvalue(),
        "sharpe_ratio": sharpe_analysis.get("sharperatio"),
        "max_drawdown_pct": drawdown_analysis.get("max", {}).get("drawdown"),
    }


def analyze_ticker(ticker: str, df: pd.DataFrame) -> dict:
    """Run full indicator, signal, and backtest analytics for a single ticker."""
    validate_dataframe_length(df, ticker)

    metrics = extract_latest_metrics(
        df,
        OPTIMIZED_SMA_PERIOD,
        RSI_PERIOD,
        ATR_PERIOD,
        VOLUME_SMA_PERIOD,
    )
    position_state = derive_position_state(
        df,
        OPTIMIZED_SMA_PERIOD,
        RSI_PERIOD,
        ATR_PERIOD,
        VOLUME_SMA_PERIOD,
        ATR_MULTIPLIER,
        RSI_BUY_THRESHOLD,
        RSI_SELL_THRESHOLD,
        end_index=len(df) - 2,
    )
    signal_result = evaluate_live_signal(
        metrics,
        position_state,
        atr_multiplier=ATR_MULTIPLIER,
        rsi_buy_threshold=RSI_BUY_THRESHOLD,
        rsi_sell_threshold=RSI_SELL_THRESHOLD,
    )
    baseline = run_backtest(
        load_backtrader_feed(df),
        enable_dynamic_atr_stop=False,
        enable_volume_filter=False,
    )
    with_risk = run_backtest(
        load_backtrader_feed(df),
        enable_dynamic_atr_stop=True,
        enable_volume_filter=True,
    )

    return {
        "ticker": ticker,
        "bar_count": len(df),
        "data_path": data_path_for_ticker(ticker),
        "metrics": metrics,
        "position_state": position_state,
        "signal_result": signal_result,
        "baseline": baseline,
        "with_risk": with_risk,
    }


def print_metrics_report(ticker: str, metrics: dict) -> None:
    """Print today and yesterday indicator values for a single ticker."""
    print(f"Latest Technical Metrics - {ticker}")
    print("=" * 88)
    for label in ("yesterday", "today"):
        row = metrics[label]
        print(
            f"{label.capitalize():<10} {row['date']} | "
            f"Close: ${row['close']:>8.2f} | "
            f"SMA({OPTIMIZED_SMA_PERIOD}): ${row['sma']:>8.2f} | "
            f"RSI({RSI_PERIOD}): {row['rsi']:>6.2f} | "
            f"ATR({ATR_PERIOD}): ${row['atr']:>7.2f} | "
            f"Vol: {row['volume']:>12,.0f} | "
            f"VolSMA({VOLUME_SMA_PERIOD}): {row['volume_sma']:>12,.0f}"
        )
    print("=" * 88)


def print_liquidity_filter_report(ticker: str, metrics: dict, signal_result: dict) -> None:
    """Print volume liquidity filter status for a single ticker."""
    today = metrics["today"]
    liquidity_ok = signal_result.get("liquidity_ok", True)
    status = "PASS" if liquidity_ok else "FAIL - INSUFFICIENT INSTITUTIONAL LIQUIDITY"

    print(f"Volume Liquidity Filter - {ticker}")
    print("=" * 88)
    print(f"Latest Volume          : {today['volume']:,.0f}")
    print(f"Volume SMA ({VOLUME_SMA_PERIOD})      : {today['volume_sma']:,.0f}")
    print(f"Liquidity Gate Status  : {status}")
    if signal_result.get("liquidity_blocked"):
        print("Entry Signal Action    : BUY INVALIDATED - HELD TO HOLD")
    print("=" * 88)


def print_dynamic_atr_position_report(ticker: str, position_state: dict) -> None:
    """Print active dynamic ATR stop state when a long position is open."""
    if not position_state["in_position"]:
        return

    print(f"Dynamic ATR Stop Position State - {ticker}")
    print("=" * 88)
    print(
        f"Captured Peak           : "
        f"${position_state['highest_price_achieved']:,.2f}"
    )
    print(
        f"Current ATR ({ATR_PERIOD})       : "
        f"${position_state['current_atr']:,.2f}"
    )
    print(
        f"Dynamic Stop Distance   : "
        f"${position_state['dynamic_stop_distance']:,.2f} "
        f"(ATR x {ATR_MULTIPLIER:.1f})"
    )
    print(
        f"Active Trigger Floor    : "
        f"${position_state['trigger_floor']:,.2f}"
    )
    print("=" * 88)


def print_dynamic_atr_liquidation_log(ticker: str, dynamic_atr_stop: dict) -> None:
    """Log dynamic ATR stop breach telemetry to the console."""
    print(f"Dynamic ATR Volatility Liquidation Telemetry - {ticker}")
    print("=" * 88)
    print(
        f"Captured Peak           : "
        f"${dynamic_atr_stop['captured_peak']:,.2f}"
    )
    print(
        f"Current ATR             : "
        f"${dynamic_atr_stop['current_atr']:,.2f}"
    )
    print(
        f"Dynamic Stop Distance   : "
        f"${dynamic_atr_stop['dynamic_stop_distance']:,.2f}"
    )
    print(
        f"Breached Trigger Floor  : "
        f"${dynamic_atr_stop['trigger_floor']:,.2f}"
    )
    print(
        f"Current Execution Price : "
        f"${dynamic_atr_stop['current_execution_price']:,.2f}"
    )
    print("=" * 88)


def print_live_signal_banner(ticker: str, signal: str) -> None:
    """Render the active live trading signal as a terminal banner."""
    if signal == "DYNAMIC_ATR_SELL":
        banner_text = (
            f"LIVE SIGNAL: SELL {ticker} - DYNAMIC ATR VOLATILITY LIQUIDATION"
        )
    elif signal == "BUY":
        banner_text = f"LIVE SIGNAL: BUY {ticker}"
    elif signal == "SELL":
        banner_text = f"LIVE SIGNAL: SELL {ticker}"
    else:
        banner_text = f"LIVE SIGNAL: HOLD/HOLDING POSITION - {ticker}"

    width = max(len(banner_text) + 4, 88)
    border = "=" * width
    print()
    print(border)
    print(f"[{banner_text}]".center(width))
    print(border)
    print()


def _format_metric(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{value:.4f}{suffix}" if suffix == "" else f"{value:.2f}{suffix}"


def print_backtest_report(ticker: str, baseline: dict, with_risk: dict) -> None:
    """Print baseline vs risk-managed backtest comparison for a ticker."""
    print(f"Backtest Performance Summary - {ticker}")
    print("=" * 88)
    print(f"{'Metric':<28} {'Baseline':>26} {'Risk-Managed':>26}")
    print("-" * 88)
    print(
        f"{'Final Portfolio Value':<28} "
        f"${baseline['final_value']:>24,.2f} "
        f"${with_risk['final_value']:>24,.2f}"
    )
    print(
        f"{'Sharpe Ratio':<28} "
        f"{_format_metric(baseline['sharpe_ratio']):>26} "
        f"{_format_metric(with_risk['sharpe_ratio']):>26}"
    )
    print(
        f"{'Max Drawdown (MDD)':<28} "
        f"{_format_metric(baseline['max_drawdown_pct'], '%'):>26} "
        f"{_format_metric(with_risk['max_drawdown_pct'], '%'):>26}"
    )
    print("=" * 88)


def print_ticker_session_report(result: dict) -> None:
    """Print consolidated telemetry and signal banner for one ticker."""
    ticker = result["ticker"]
    signal_result = result["signal_result"]

    print()
    print("#" * 88)
    print(f"SESSION REPORT: {ticker}".center(88))
    print("#" * 88)

    print_metrics_report(ticker, result["metrics"])
    print_liquidity_filter_report(ticker, result["metrics"], signal_result)
    print_dynamic_atr_position_report(ticker, result["position_state"])

    if signal_result["signal"] == "DYNAMIC_ATR_SELL":
        print_dynamic_atr_liquidation_log(ticker, signal_result["dynamic_atr_stop"])

    print_live_signal_banner(ticker, signal_result["signal"])
    print_backtest_report(ticker, result["baseline"], result["with_risk"])


def main() -> None:
    print("Multi-Asset Live Signal Generator Pipeline")
    print(f"Execution Timestamp : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Watchlist           : {', '.join(WATCHLIST)}")
    print(
        f"Dynamic ATR Engine  : ATR({ATR_PERIOD}) x {ATR_MULTIPLIER:.1f} "
        f"volatility buffer"
    )
    print(
        f"Volume Liquidity    : Volume SMA({VOLUME_SMA_PERIOD}) entry gate"
    )
    print("-" * 88)

    session_results = []

    for ticker in WATCHLIST:
        data_path = data_path_for_ticker(ticker)
        print(f"Ingesting {ticker}...", end=" ")
        df = fetch_market_data(ticker, LOOKBACK_YEARS)
        persist_market_data(df, data_path)
        print(f"{len(df)} bars saved to {data_path}")
        session_results.append(analyze_ticker(ticker, df))

    print()
    print("=" * 88)
    print("WATCHLIST SESSION COMPLETE - LIVE SIGNAL OUTPUT".center(88))
    print("=" * 88)

    for result in session_results:
        print_ticker_session_report(result)

    print()
    print("=" * 88)
    print("WATCHLIST SIGNAL SUMMARY".center(88))
    print("=" * 88)
    for result in session_results:
        signal = result["signal_result"]["signal"]
        print(f"  {result['ticker']:<6} -> {signal}")
    print("=" * 88)


if __name__ == "__main__":
    main()
