"""
Validate SPY 200MA vs optional entry-confirmation / VIX overlays.

Priority: realistic costs (commission + slippage) BEFORE judging filters.
Does NOT change production defaults — research script only.

Usage:
  python scripts/filter_combo_backtest.py
  FILTER_START=2020-01-01 FILTER_END=2026-12-31 python scripts/filter_combo_backtest.py
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import pandas as pd

from config import StrategyConfigMapper
from market_registry import (
    BENCHMARK_TICKER,
    DEFAULT_WATCHLIST,
    SECONDARY_BENCHMARK_TICKER,
    parse_watchlist,
)
from momentum_ranker import MomentumRankSettings
from portfolio_backtest import run_portfolio_backtest
from run_backtest import (
    YFINANCE_WARMUP_START,
    fetch_yfinance_ohlcv,
    load_vix_frame,
    load_yfinance_watchlist_data,
    slice_ohlcv_window,
)

# 1순위: 비용 현실화 기본값 (백테스트 판단은 항상 이 비용 위에서)
REALISTIC_COMMISSION = float(os.getenv("FILTER_COMMISSION", "0.001"))
REALISTIC_SLIPPAGE_BPS = float(os.getenv("FILTER_SLIPPAGE_BPS", "5"))
WINDOW_START = os.getenv("FILTER_START", "2020-01-01")
WINDOW_END = os.getenv("FILTER_END", "2026-12-31")
INITIAL_CASH = float(os.getenv("FILTER_CASH", "100000"))

SCENARIOS: list[tuple[str, dict[str, str]]] = [
    (
        "SPY_200MA_only",
        {"ENTRY_CONFIRMATION_DAYS": "0", "USE_VIX_REGIME_FILTER": "false"},
    ),
    (
        "SPY_200MA + entry_confirm_3d",
        {"ENTRY_CONFIRMATION_DAYS": "3", "USE_VIX_REGIME_FILTER": "false"},
    ),
    (
        "SPY_200MA + VIX<=25",
        {
            "ENTRY_CONFIRMATION_DAYS": "0",
            "USE_VIX_REGIME_FILTER": "true",
            "VIX_REGIME_MAX": "25",
        },
    ),
    (
        "SPY_200MA + entry_confirm_3d + VIX<=25",
        {
            "ENTRY_CONFIRMATION_DAYS": "3",
            "USE_VIX_REGIME_FILTER": "true",
            "VIX_REGIME_MAX": "25",
        },
    ),
]


@contextmanager
def env_overlay(overrides: dict[str, str]):
    prior = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, old in prior.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _years_in_window(start: str, end: str) -> float:
    delta = pd.Timestamp(end) - pd.Timestamp(start)
    return max(delta.days / 365.25, 1 / 365.25)


def _cagr_pct(initial: float, final: float, years: float) -> float:
    if initial <= 0 or final <= 0 or years <= 0:
        return 0.0
    return ((final / initial) ** (1.0 / years) - 1.0) * 100.0


def _run_legacy_scenario(
    label: str,
    env_overrides: dict[str, str],
    *,
    loaded: list[str],
    ohlcv: dict[str, pd.DataFrame],
    spy_df: pd.DataFrame | None,
    qqq_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
    years: float,
) -> dict[str, float | int | str]:
    legacy_momentum = MomentumRankSettings(
        enabled=False,
        top_n=3,
        rebalance_weekday=4,
        min_bars=60,
    )
    with env_overlay(env_overrides):
        use_vix = StrategyConfigMapper.use_vix_regime_filter()
        entry_days = StrategyConfigMapper.entry_confirmation_days()
        result = run_portfolio_backtest(
            tickers=loaded,
            ohlcv_by_ticker=ohlcv,
            initial_cash=INITIAL_CASH,
            commission_rate=REALISTIC_COMMISSION,
            slippage_bps=REALISTIC_SLIPPAGE_BPS,
            use_spy_market_filter=True,
            spy_df=spy_df,
            qqq_df=qqq_df,
            vix_df=vix_df if use_vix else None,
            momentum_settings=legacy_momentum,
        )
    return {
        "scenario": label,
        "entry_confirm_days": entry_days,
        "vix_filter": use_vix,
        "total_return_pct": result.total_return_pct,
        "cagr_pct": _cagr_pct(result.initial_cash, result.final_equity, years),
        "max_drawdown_pct": result.max_drawdown_pct,
        "sharpe_ratio": result.sharpe_ratio,
        "total_trades": result.total_trades,
        "winning_trades": result.winning_trades,
        "final_equity": result.final_equity,
    }


def main() -> int:
    tickers = parse_watchlist(os.getenv("WATCHLIST", ",".join(DEFAULT_WATCHLIST)))
    print("Loading yfinance OHLCV...")
    full_ohlcv, skipped = load_yfinance_watchlist_data(
        tickers,
        start=YFINANCE_WARMUP_START,
    )
    if skipped:
        for line in skipped:
            print(f"  skip: {line}")

    ohlcv = {
        t: slice_ohlcv_window(full_ohlcv[t], WINDOW_START, WINDOW_END)
        for t in full_ohlcv
        if not slice_ohlcv_window(full_ohlcv[t], WINDOW_START, WINDOW_END).empty
    }
    loaded = list(ohlcv.keys())
    if len(loaded) < 2:
        print("Need at least 2 tickers in window.")
        return 1

    spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
    qqq_df = None
    if StrategyConfigMapper.use_qqq_regime_filter():
        qqq_df = fetch_yfinance_ohlcv(
            SECONDARY_BENCHMARK_TICKER,
            start=YFINANCE_WARMUP_START,
        )
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)
    if vix_df is None:
        from vix_data import fetch_vix_daily_yfinance

        vix_df = fetch_vix_daily_yfinance(start=YFINANCE_WARMUP_START)

    years = _years_in_window(WINDOW_START, WINDOW_END)
    print()
    print("=" * 96)
    print("LEGACY ENGINE - SPY 200MA FILTER COMBO VALIDATION (research only)".center(96))
    print("=" * 96)
    print(f"Window           : {WINDOW_START} -> {WINDOW_END} ({years:.2f} yr)")
    print(f"Capital          : ${INITIAL_CASH:,.0f}")
    print(f"Tickers          : {len(loaded)}")
    print(
        f"Cost model       : commission {REALISTIC_COMMISSION * 100:.2f}% + "
        f"slippage {REALISTIC_SLIPPAGE_BPS:.0f} bps (1-way)"
    )
    print("Production lock  : SPY 200MA only (ENTRY_CONFIRMATION_DAYS=0, USE_VIX_REGIME_FILTER=false)")
    print()

    rows: list[dict[str, float | int | str]] = []
    for label, overrides in SCENARIOS:
        print(f"Running {label}...")
        rows.append(
            _run_legacy_scenario(
                label,
                overrides,
                loaded=loaded,
                ohlcv=ohlcv,
                spy_df=spy_df,
                qqq_df=qqq_df,
                vix_df=vix_df,
                years=years,
            )
        )

    baseline = rows[0]
    header = (
        f"{'Scenario':<38} {'CAGR%':>7} {'Return%':>9} {'MaxDD%':>8} "
        f"{'Sharpe':>7} {'Trades':>7} {'vs base':>10}"
    )
    print()
    print(header)
    print("-" * len(header))
    for row in rows:
        delta_cagr = float(row["cagr_pct"]) - float(baseline["cagr_pct"])
        delta_dd = float(row["max_drawdown_pct"]) - float(baseline["max_drawdown_pct"])
        vs = f"{delta_cagr:+.1f}pp / {delta_dd:+.1f}pp"
        print(
            f"{str(row['scenario']):<38} "
            f"{float(row['cagr_pct']):>+6.1f} "
            f"{float(row['total_return_pct']):>+8.1f} "
            f"{float(row['max_drawdown_pct']):>8.1f} "
            f"{float(row['sharpe_ratio']):>7.2f} "
            f"{int(row['total_trades']):>7} "
            f"{vs:>10}"
        )
    print()
    print("vs base = CAGR delta / MaxDD delta vs SPY_200MA_only")
    print("Interpretation: lower MaxDD with lower CAGR => late-entry tax; enable only if tradeoff acceptable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
