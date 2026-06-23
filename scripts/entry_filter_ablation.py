"""
Entry-filter ablation: weekly trend / 52w-high / scale-in / scale-out.

Each filter is toggled independently against the prod baseline (all ON).
Dual 70/30 fixed (matching production).

Sub-periods reported separately:
  2020-2021  bull run
  2022       bear / drawdown
  2023-2024  recovery + bull
  2025-2026  transition

Usage:
  python scripts/entry_filter_ablation.py
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from config import StrategyConfigMapper
from deployment_config import scaled_capital
from market_registry import BENCHMARK_TICKER, SECONDARY_BENCHMARK_TICKER, parse_watchlist
from momentum_ranker import MomentumRankSettings
from portfolio_backtest import compute_max_drawdown, compute_sharpe_ratio, run_portfolio_backtest
from run_backtest import (
    YFINANCE_WARMUP_START,
    fetch_yfinance_ohlcv,
    load_vix_frame,
    load_yfinance_watchlist_data,
    slice_ohlcv_window,
)
from top3_backtest import run_top3_backtest
from trading_features import TradingFeatureFlags

# ── constants ────────────────────────────────────────────────────────────────
FULL_START = "2020-01-01"
FULL_END   = "2025-12-31"
CASH       = 100_000.0
LEG_PCT    = 70.0
TOP_PCT    = 30.0
COMMISSION = 0.001
SLIPPAGE   = 5.0

SUB_PERIODS = [
    ("2020-21 bull", "2020-01-01", "2021-12-31"),
    ("2022 bear",    "2022-01-01", "2022-12-31"),
    ("2023-24 bull", "2023-01-01", "2024-12-31"),
    ("2025-26 trans","2025-01-01", "2025-12-31"),
]

# ── scenario definitions ──────────────────────────────────────────────────────
# Each entry: (label, TradingFeatureFlags kwargs override)
SCENARIOS: list[tuple[str, dict]] = [
    # ── baseline (prod) ────────────────────────────────────────────────────
    ("baseline (prod)",
     dict(use_weekly_trend_filter=True,  use_52w_high_filter=True,
          near_52w_high_pct=0.05,
          use_scale_in=True,             use_scale_out=True)),
    # ── weekly trend ablations ─────────────────────────────────────────────
    ("weekly=OFF",
     dict(use_weekly_trend_filter=False, use_52w_high_filter=True,
          near_52w_high_pct=0.05,
          use_scale_in=True,             use_scale_out=True)),
    # ── 52w-high ablations ─────────────────────────────────────────────────
    ("52w=OFF",
     dict(use_weekly_trend_filter=True,  use_52w_high_filter=False,
          near_52w_high_pct=0.05,
          use_scale_in=True,             use_scale_out=True)),
    ("52w pct=3%",
     dict(use_weekly_trend_filter=True,  use_52w_high_filter=True,
          near_52w_high_pct=0.03,
          use_scale_in=True,             use_scale_out=True)),
    ("52w pct=8%",
     dict(use_weekly_trend_filter=True,  use_52w_high_filter=True,
          near_52w_high_pct=0.08,
          use_scale_in=True,             use_scale_out=True)),
    ("52w pct=10%",
     dict(use_weekly_trend_filter=True,  use_52w_high_filter=True,
          near_52w_high_pct=0.10,
          use_scale_in=True,             use_scale_out=True)),
    # ── both OFF ───────────────────────────────────────────────────────────
    ("weekly+52w=OFF",
     dict(use_weekly_trend_filter=False, use_52w_high_filter=False,
          near_52w_high_pct=0.05,
          use_scale_in=True,             use_scale_out=True)),
    # ── scale-in / scale-out ablations ────────────────────────────────────
    ("scale_in=OFF",
     dict(use_weekly_trend_filter=True,  use_52w_high_filter=True,
          near_52w_high_pct=0.05,
          use_scale_in=False,            use_scale_out=True)),
    ("scale_out=OFF",
     dict(use_weekly_trend_filter=True,  use_52w_high_filter=True,
          near_52w_high_pct=0.05,
          use_scale_in=True,             use_scale_out=False)),
    ("scale_in+out=OFF",
     dict(use_weekly_trend_filter=True,  use_52w_high_filter=True,
          near_52w_high_pct=0.05,
          use_scale_in=False,            use_scale_out=False)),
    # ── all filters OFF ────────────────────────────────────────────────────
    ("all filters OFF",
     dict(use_weekly_trend_filter=False, use_52w_high_filter=False,
          near_52w_high_pct=0.05,
          use_scale_in=False,            use_scale_out=False)),
]


# ── helpers ───────────────────────────────────────────────────────────────────
def _equity_series(frame: pd.DataFrame, initial: float) -> pd.Series:
    if frame.empty or "equity" not in frame.columns:
        return pd.Series(dtype=float)
    if isinstance(frame.index, pd.DatetimeIndex):
        return frame["equity"].astype(float)
    if "date" in frame.columns:
        return pd.Series(
            frame["equity"].astype(float).values,
            index=pd.to_datetime(frame["date"]),
        )
    return frame["equity"].astype(float)


def _window_metrics(combined: pd.Series, initial: float, start: str, end: str) -> dict:
    """Metrics for a sub-window slice of a combined equity curve."""
    ts = pd.Timestamp(start)
    te = pd.Timestamp(end)
    sliced = combined.loc[ts:te]
    if sliced.empty:
        return dict(ret=0.0, sharpe=0.0, maxdd=0.0, cagr=0.0, mar=0.0)
    s0 = float(sliced.iloc[0])
    sf = float(sliced.iloc[-1])
    years = max((te - ts).days / 365.25, 1 / 365.25)
    ret = (sf / s0 - 1.0) * 100.0
    cagr = ((sf / s0) ** (1.0 / years) - 1.0) * 100.0 if s0 > 0 else 0.0
    maxdd = compute_max_drawdown(sliced)
    sharpe = compute_sharpe_ratio(sliced)
    mar = cagr / maxdd if maxdd > 0 else float("inf")
    return dict(ret=ret, sharpe=sharpe, maxdd=maxdd, cagr=cagr, mar=mar)


def _run_one(
    loaded, full_ohlcv, ohlcv_window, spy_df, qqq_df, vix_df,
    *, features: TradingFeatureFlags,
    window_start: str, window_end: str,
) -> tuple[pd.Series, int]:
    leg_cash = scaled_capital(CASH, LEG_PCT / (LEG_PCT + TOP_PCT))
    top_cash = scaled_capital(CASH, TOP_PCT / (LEG_PCT + TOP_PCT))

    legacy_mom = replace(MomentumRankSettings.from_env().for_production(), enabled=False)
    top3_mom   = replace(
        MomentumRankSettings.from_env().for_production().for_top3(),
        enabled=True, min_bars=min(MomentumRankSettings.from_env().min_bars, 60),
        dynamic_rebalance_only=False,
    )

    leg_tickers = [t for t in loaded if t in ohlcv_window]
    legacy = run_portfolio_backtest(
        tickers=leg_tickers,
        ohlcv_by_ticker={t: ohlcv_window[t] for t in leg_tickers},
        initial_cash=leg_cash,
        commission_rate=COMMISSION,
        slippage_bps=SLIPPAGE,
        use_spy_market_filter=StrategyConfigMapper.use_spy_market_filter(),
        spy_df=spy_df, qqq_df=qqq_df, vix_df=vix_df,
        momentum_settings=legacy_mom,
        features=features,
    )
    top3 = run_top3_backtest(
        tickers=loaded,
        ohlcv_by_ticker=full_ohlcv,
        initial_cash=top_cash,
        commission_rate=COMMISSION,
        slippage_bps=SLIPPAGE,
        momentum_settings=top3_mom,
        window_start=window_start,
        window_end=window_end,
    )
    leg_eq  = _equity_series(legacy.equity_curve, leg_cash)
    top_eq  = _equity_series(top3.equity_curve,   top_cash)
    combined = (
        pd.concat([leg_eq.rename("leg"), top_eq.rename("top")], axis=1, sort=True)
        .ffill()
        .fillna({"leg": leg_cash, "top": top_cash})
        .sum(axis=1)
    )
    trades = legacy.total_trades + top3.total_trades
    return combined, trades


def _print_row(label: str, m_full: dict, sub_metrics: list[dict], trades: int) -> None:
    cagr_maxdd = m_full["cagr"] / m_full["maxdd"] if m_full["maxdd"] > 0 else 99.9
    sub_sharpes = "  ".join(f"{s['sharpe']:>5.2f}" for s in sub_metrics)
    print(
        f"  {label:<22} "
        f"ret={m_full['ret']:>+7.1f}%  "
        f"cagr={m_full['cagr']:>+6.1f}%  "
        f"sharpe={m_full['sharpe']:>5.2f}  "
        f"maxdd={m_full['maxdd']:>5.1f}%  "
        f"C/D={cagr_maxdd:>4.2f}  "
        f"tr={trades:>4d}  "
        f"sub_sharpe[{sub_sharpes}]"
    )


def main() -> int:
    print("Loading yfinance data...", flush=True)
    full_watchlist = parse_watchlist(os.getenv("WATCHLIST"))
    full_ohlcv, skipped = load_yfinance_watchlist_data(full_watchlist, start=YFINANCE_WARMUP_START)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in full_watchlist if t in full_ohlcv]

    use_spy = StrategyConfigMapper.use_spy_market_filter()
    spy_df  = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START) if use_spy else None
    qqq_df  = (
        fetch_yfinance_ohlcv(SECONDARY_BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
        if use_spy and StrategyConfigMapper.use_qqq_regime_filter() else None
    )
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)

    full_window = {
        t: slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)
        for t in loaded
        if len(slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)) >= 22
    }
    loaded = [t for t in loaded if t in full_window]

    width = 120
    print()
    print("=" * width)
    print(
        f"ENTRY FILTER ABLATION  Dual {LEG_PCT:.0f}/{TOP_PCT:.0f}  "
        f"{FULL_START} → {FULL_END}".center(width)
    )
    print("=" * width)
    sub_labels = "  ".join(f"{lbl:>5}" for lbl, *_ in SUB_PERIODS)
    print(
        f"  {'Scenario':<22} "
        f"{'Return':>8}  {'CAGR':>7}  {'Sharpe':>7}  "
        f"{'MaxDD':>6}  {'C/D':>4}  {'Tr':>4}  "
        f"sub_sharpe[{sub_labels}]"
    )
    print("-" * width)

    # Pre-slice sub-period windows (just the ohlcv for legacy; top3 uses full_ohlcv)
    sub_windows = []
    for lbl, s, e in SUB_PERIODS:
        w = {
            t: slice_ohlcv_window(full_ohlcv[t], s, e)
            for t in loaded
            if len(slice_ohlcv_window(full_ohlcv[t], s, e)) >= 22
        }
        sub_windows.append((lbl, s, e, [t for t in loaded if t in w], w))

    results: list[tuple[str, dict, list[dict], int]] = []
    for label, feat_kw in SCENARIOS:
        feat = TradingFeatureFlags(
            use_vol_adjusted_risk=True,
            vol_target_pct=0.015,
            use_regime_golden_cross=True,
            regime_cautious_max_positions=2,
            **feat_kw,
        )
        print(f"  Running {label}...", flush=True)

        # full period
        combined_full, trades = _run_one(
            loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df,
            features=feat,
            window_start=FULL_START, window_end=FULL_END,
        )
        m_full = _window_metrics(combined_full, CASH, FULL_START, FULL_END)

        # sub-periods (reuse full combined curve — sub-slice it)
        sub_ms = [
            _window_metrics(combined_full, CASH, s, e)
            for _, s, e, *_ in sub_windows
        ]
        results.append((label, m_full, sub_ms, trades))
        _print_row(label, m_full, sub_ms, trades)

    # ── delta table vs baseline ───────────────────────────────────────────────
    print()
    print("DELTA vs baseline (CAGR / Sharpe / MaxDD / trades)".center(width))
    print("-" * width)
    base_m, base_sub, base_tr = results[0][1], results[0][2], results[0][3]
    for label, m, sub_ms, trades in results[1:]:
        d_cagr   = m["cagr"]   - base_m["cagr"]
        d_sharpe = m["sharpe"] - base_m["sharpe"]
        d_maxdd  = m["maxdd"]  - base_m["maxdd"]
        d_tr     = trades      - base_tr
        sub_d = "  ".join(
            f"{(s['sharpe'] - bs['sharpe']):>+5.2f}"
            for s, bs in zip(sub_ms, base_sub)
        )
        print(
            f"  {label:<22} "
            f"ΔCAGR={d_cagr:>+6.2f}pp  "
            f"ΔSharpe={d_sharpe:>+5.2f}  "
            f"ΔMaxDD={d_maxdd:>+5.2f}pp  "
            f"ΔTr={d_tr:>+4d}  "
            f"Δsub_sharpe[{sub_d}]"
        )

    print("=" * width)
    # ── recommendation ────────────────────────────────────────────────────────
    # Best CAGR/MaxDD (MAR proxy) excluding baseline
    best_label, best_m, _, _ = max(results[1:], key=lambda x: x[1]["cagr"] / max(x[1]["maxdd"], 0.01))
    base_mar = base_m["cagr"] / max(base_m["maxdd"], 0.01)
    best_mar = best_m["cagr"] / max(best_m["maxdd"], 0.01)
    margin   = best_mar - base_mar
    if margin > 0.02:
        print(f"Recommendation: consider '{best_label}' — MAR {best_mar:.3f} vs baseline {base_mar:.3f} (+{margin:.3f}).")
    else:
        print(f"Recommendation: keep prod baseline -- no challenger beats it by >0.02 MAR (best alt: '{best_label}', delta={margin:+.3f}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
