"""Commission × slippage grid for Phase 4 dual backtest (legacy + Top3)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import pandas as pd

from config import StrategyConfigMapper
from deployment_config import DeploymentConfig, scaled_capital
from market_registry import BENCHMARK_TICKER, DEFAULT_WATCHLIST, parse_watchlist
from momentum_ranker import MomentumRankSettings
from portfolio_backtest import run_portfolio_backtest
from run_backtest import (
    YFINANCE_WARMUP_START,
    fetch_yfinance_ohlcv,
    load_vix_frame,
    load_yfinance_watchlist_data,
    slice_ohlcv_window,
)
from top3_backtest import run_top3_backtest, summarize_dual_combined

COMMISSION_GRID = (0.001, 0.003, 0.005)
SLIPPAGE_GRID = (0.0, 5.0, 10.0)
WINDOW_START = os.getenv("COST_SENS_START", "2020-01-01")
WINDOW_END = os.getenv("COST_SENS_END", "2026-12-31")
TOTAL_CASH = float(os.getenv("CAPITAL_AT_RISK", "100000"))


def _dual_metrics(
    loaded: list[str],
    ohlcv: dict[str, pd.DataFrame],
    full_ohlcv: dict[str, pd.DataFrame],
    spy_df: pd.DataFrame | None,
    qqq_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
    *,
    commission: float,
    slippage_bps: float,
) -> dict[str, float]:
    deploy = DeploymentConfig.from_env()
    if not deploy.is_dual:
        deploy = DeploymentConfig(
            phase=4,
            strategy_mode="dual",
            top3_backtest_only=False,
            top3_dry_run_enabled=False,
            legacy_capital_pct=float(os.getenv("LEGACY_CAPITAL_PCT", "60")),
            top3_capital_pct=float(os.getenv("TOP3_CAPITAL_PCT", "40")),
        )
    leg_cash = scaled_capital(TOTAL_CASH, deploy.legacy_capital_fraction())
    top_cash = scaled_capital(TOTAL_CASH, deploy.top3_capital_fraction())

    legacy_momentum = MomentumRankSettings(
        enabled=False,
        top_n=3,
        rebalance_weekday=4,
        min_bars=60,
    )
    top3_settings = MomentumRankSettings.from_env().for_top3()

    legacy = run_portfolio_backtest(
        tickers=loaded,
        ohlcv_by_ticker=ohlcv,
        initial_cash=leg_cash,
        commission_rate=commission,
        slippage_bps=slippage_bps,
        use_spy_market_filter=spy_df is not None,
        spy_df=spy_df,
        qqq_df=qqq_df,
        vix_df=vix_df,
        momentum_settings=legacy_momentum,
    )
    top3 = run_top3_backtest(
        tickers=loaded,
        ohlcv_by_ticker=full_ohlcv,
        initial_cash=top_cash,
        commission_rate=commission,
        slippage_bps=slippage_bps,
        momentum_settings=top3_settings,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )
    combined = summarize_dual_combined(
        legacy,
        top3,
        legacy_pct=deploy.legacy_capital_pct,
        top3_pct=deploy.top3_capital_pct,
    )
    return {
        "return_pct": float(combined["total_return_pct"]),
        "max_dd_pct": float(combined["max_drawdown_pct"]),
        "sharpe": float(combined["sharpe_ratio"]),
    }


def main() -> int:
    tickers = parse_watchlist(os.getenv("WATCHLIST", ",".join(DEFAULT_WATCHLIST)))
    print(f"Loading yfinance data for {len(tickers)} tickers...")
    full_ohlcv, skipped = load_yfinance_watchlist_data(
        tickers,
        start=YFINANCE_WARMUP_START,
    )
    if skipped:
        for line in skipped:
            print(f"  skip: {line}")

    loaded = [t for t in tickers if t in full_ohlcv]
    ohlcv = {
        t: slice_ohlcv_window(full_ohlcv[t], WINDOW_START, WINDOW_END)
        for t in loaded
        if not slice_ohlcv_window(full_ohlcv[t], WINDOW_START, WINDOW_END).empty
    }
    loaded = list(ohlcv.keys())
    if len(loaded) < 2:
        print("Need at least 2 tickers with data in window.")
        return 1

    spy_df = None
    qqq_df = None
    if StrategyConfigMapper.use_spy_market_filter():
        spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
        if StrategyConfigMapper.use_qqq_regime_filter():
            from market_registry import SECONDARY_BENCHMARK_TICKER

            qqq_df = fetch_yfinance_ohlcv(
                SECONDARY_BENCHMARK_TICKER,
                start=YFINANCE_WARMUP_START,
            )
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)

    print(
        f"\nPhase 4 dual cost grid | ${TOTAL_CASH:,.0f} | "
        f"{WINDOW_START} → {WINDOW_END} | {len(loaded)} tickers\n"
    )
    header = (
        f"{'Commission':>10} {'Slip bps':>9} {'Return%':>10} "
        f"{'MaxDD%':>8} {'Sharpe':>7}"
    )
    print(header)
    print("-" * len(header))

    rows: list[dict[str, float]] = []
    for commission in COMMISSION_GRID:
        for slip in SLIPPAGE_GRID:
            metrics = _dual_metrics(
                loaded,
                ohlcv,
                full_ohlcv,
                spy_df,
                qqq_df,
                vix_df,
                commission=commission,
                slippage_bps=slip,
            )
            rows.append({"commission": commission, "slippage_bps": slip, **metrics})
            print(
                f"{commission * 100:>9.1f}% {slip:>9.1f} "
                f"{metrics['return_pct']:>+9.1f} "
                f"{metrics['max_dd_pct']:>8.1f} "
                f"{metrics['sharpe']:>7.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
