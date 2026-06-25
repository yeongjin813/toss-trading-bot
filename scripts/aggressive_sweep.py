"""
Phase 24: Aggressive vs Prod backtest comparison.

Compares three configurations head-to-head across the full 2020-2025 period:
  Config A: Current PROD (conservative baseline)
  Config B: Aggressive (수익 극대화 — lower RSI/volume bars, more positions, fixed sizing)
  Config C: Semi-aggressive (middle ground — looser bars, keep vol scaling)

Dual 70/30 Legacy/Top4. Full period + OOS (2023-25) + 4 sub-periods.

Key risk question: does aggressive config survive bear2022?

Usage:
  python scripts/aggressive_sweep.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from config import StrategyConfigMapper, TickerConfig
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

FULL_START = "2020-01-01"
FULL_END   = "2025-12-31"
OOS_START  = "2023-01-01"
OOS_END    = "2025-12-31"

CASH       = 100_000.0
LEG_PCT    = 70.0
TOP_PCT    = 30.0
COMMISSION = 0.001
SLIPPAGE   = 5.0

SUB_PERIODS = [
    ("bull20-21", "2020-01-01", "2021-12-31"),
    ("bear2022",  "2022-01-01", "2022-12-31"),
    ("bull23-24", "2023-01-01", "2024-12-31"),
    ("trans2025", "2025-01-01", "2025-12-31"),
]

# Config tuples:
# (label, vol_default, vol_mega, vol_high_beta, vol_momentum,
#  rsi_default, rsi_mega, rsi_high_beta, rsi_momentum,
#  regime_cautious_max_pos, use_regime_golden_cross, use_vol_adjusted_risk,
#  max_open_positions, description)
#
# Note: MEGA tickers (AAPL/MSFT/GOOGL/AMZN) use vol_mega separately from DEFAULT.
# Prod has MEGA vol=0.65 (same as DEFAULT). For aggressive, all = 0.45.
CONFIGS = [
    (
        "A: PROD",
        0.65, 0.65, 0.55, 0.60,   # vol: default, mega, high_beta, momentum
        48.0, 45.0, 40.0, 40.0,   # rsi: default, mega, high_beta, momentum
        2, True,  True,  5,       # cautious_max_pos, golden_cross, vol_adj_risk, max_open
        "Conservative baseline (current prod)",
    ),
    (
        "B: Aggressive",
        0.45, 0.45, 0.45, 0.45,
        40.0, 38.0, 35.0, 35.0,
        4, False, False, 7,
        "Max returns: lower bars, 4 cautious-pos, fixed sizing",
    ),
    (
        "C: Semi-aggressive",
        0.50, 0.50, 0.50, 0.50,
        44.0, 42.0, 38.0, 38.0,
        3, False, True,  6,
        "Middle ground: looser bars, keep vol scaling",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _window_metrics(combined: pd.Series, start: str, end: str) -> dict:
    sliced = combined.loc[pd.Timestamp(start):pd.Timestamp(end)]
    if len(sliced) < 2:
        return dict(cagr=0.0, sharpe=0.0, maxdd=0.0, ret=0.0)
    s0, sf = float(sliced.iloc[0]), float(sliced.iloc[-1])
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1 / 365.25)
    cagr = ((sf / s0) ** (1.0 / years) - 1.0) * 100.0 if s0 > 0 else 0.0
    ret  = (sf / s0 - 1.0) * 100.0 if s0 > 0 else 0.0
    return dict(
        cagr=cagr,
        sharpe=compute_sharpe_ratio(sliced),
        maxdd=compute_max_drawdown(sliced),
        ret=ret,
    )


def _patch_strategy_configs(
    vol_default: float,
    vol_mega: float,
    vol_high_beta: float,
    vol_momentum: float,
    rsi_default: float,
    rsi_mega: float,
    rsi_high_beta: float,
    rsi_momentum: float,
):
    """Temporarily override StrategyConfigMapper class-level TickerConfig presets.

    Returns a restore() callable. Call it in a finally block.
    """
    orig_default   = StrategyConfigMapper._DEFAULT
    orig_mega      = StrategyConfigMapper._MEGA
    orig_high_beta = StrategyConfigMapper._HIGH_BETA
    orig_momentum  = StrategyConfigMapper._MOMENTUM
    orig_explicit  = StrategyConfigMapper._EXPLICIT

    new_default   = replace(orig_default,   rsi_buy_threshold=rsi_default,   volume_threshold=vol_default)
    new_mega      = replace(orig_mega,      rsi_buy_threshold=rsi_mega,      volume_threshold=vol_mega)
    new_high_beta = replace(orig_high_beta, rsi_buy_threshold=rsi_high_beta, volume_threshold=vol_high_beta)
    new_momentum  = replace(orig_momentum,  rsi_buy_threshold=rsi_momentum,  volume_threshold=vol_momentum)

    StrategyConfigMapper._DEFAULT   = new_default
    StrategyConfigMapper._MEGA      = new_mega
    StrategyConfigMapper._HIGH_BETA = new_high_beta
    StrategyConfigMapper._MOMENTUM  = new_momentum
    StrategyConfigMapper._EXPLICIT  = {
        "AAPL":  new_mega,
        "MSFT":  new_mega,
        "GOOGL": new_mega,
        "AMZN":  new_mega,
        "NVDA":  new_high_beta,
        "META":  new_high_beta,
        "AVGO":  new_high_beta,
        "NFLX":  new_high_beta,
        "PLTR":  new_momentum,
        "TSLA":  new_momentum,
        "CRWD":  new_momentum,
        "AMD":   new_momentum,
    }

    def restore() -> None:
        StrategyConfigMapper._DEFAULT   = orig_default
        StrategyConfigMapper._MEGA      = orig_mega
        StrategyConfigMapper._HIGH_BETA = orig_high_beta
        StrategyConfigMapper._MOMENTUM  = orig_momentum
        StrategyConfigMapper._EXPLICIT  = orig_explicit

    return restore


def _run_config(
    loaded: list[str],
    full_ohlcv: dict,
    full_window: dict,
    spy_df,
    qqq_df,
    vix_df,
    *,
    config_tuple: tuple,
) -> tuple[pd.Series, int]:
    """Run dual 70/30 backtest for one config. Returns (combined equity series, total trades)."""
    (
        label,
        vol_default, vol_mega, vol_high_beta, vol_momentum,
        rsi_default, rsi_mega, rsi_high_beta, rsi_momentum,
        regime_cautious_max_pos,
        use_regime_golden_cross,
        use_vol_adjusted_risk,
        max_open_positions,
        desc,
    ) = config_tuple

    restore = _patch_strategy_configs(
        vol_default, vol_mega, vol_high_beta, vol_momentum,
        rsi_default, rsi_mega, rsi_high_beta, rsi_momentum,
    )
    prev_max_open = os.environ.get("MAX_OPEN_POSITIONS")
    os.environ["MAX_OPEN_POSITIONS"] = str(max_open_positions)

    try:
        features = TradingFeatureFlags(
            use_vol_adjusted_risk=use_vol_adjusted_risk,
            vol_target_pct=0.015,
            use_regime_golden_cross=use_regime_golden_cross,
            regime_cautious_max_positions=regime_cautious_max_pos,
            use_scale_in=True,
            use_scale_out=True,
            use_weekly_trend_filter=True,
            use_52w_high_filter=False,   # Prod: 52w-high filter OFF (Phase 21)
            near_52w_high_pct=0.05,
        )

        leg_cash = scaled_capital(CASH, LEG_PCT / (LEG_PCT + TOP_PCT))
        top_cash = scaled_capital(CASH, TOP_PCT / (LEG_PCT + TOP_PCT))

        # Legacy leg: momentum ranking disabled (signal-driven entry)
        legacy_mom = replace(MomentumRankSettings.from_env().for_production(), enabled=False)

        # Top4 leg: momentum ranking enabled, Top4 + band=1 (prod settings)
        top3_mom = replace(
            MomentumRankSettings.from_env().for_production().for_top3(),
            enabled=True,
            top_n=4,
            dynamic_rebalance_only=False,
            top_n_hold_band=1,
            min_bars=min(MomentumRankSettings.from_env().min_bars, 60),
        )

        leg_tickers = [t for t in loaded if t in full_window]
        legacy = run_portfolio_backtest(
            tickers=leg_tickers,
            ohlcv_by_ticker={t: full_window[t] for t in leg_tickers},
            initial_cash=leg_cash,
            commission_rate=COMMISSION,
            slippage_bps=SLIPPAGE,
            use_spy_market_filter=StrategyConfigMapper.use_spy_market_filter(),
            spy_df=spy_df,
            qqq_df=qqq_df,
            vix_df=vix_df,
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
            window_start=FULL_START,
            window_end=FULL_END,
        )

        leg_eq = _equity_series(legacy.equity_curve, leg_cash)
        top_eq = _equity_series(top3.equity_curve, top_cash)
        combined = (
            pd.concat([leg_eq.rename("leg"), top_eq.rename("top")], axis=1, sort=True)
            .ffill()
            .fillna({"leg": leg_cash, "top": top_cash})
            .sum(axis=1)
        )
        total_trades = legacy.total_trades + top3.total_trades
        return combined, total_trades

    finally:
        restore()
        if prev_max_open is None:
            os.environ.pop("MAX_OPEN_POSITIONS", None)
        else:
            os.environ["MAX_OPEN_POSITIONS"] = prev_max_open


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Loading yfinance data...", flush=True)
    full_watchlist = parse_watchlist(os.getenv("WATCHLIST"))
    full_ohlcv, skipped = load_yfinance_watchlist_data(full_watchlist, start=YFINANCE_WARMUP_START)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in full_watchlist if t in full_ohlcv]

    use_spy = StrategyConfigMapper.use_spy_market_filter()
    spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START) if use_spy else None
    qqq_df = (
        fetch_yfinance_ohlcv(SECONDARY_BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
        if use_spy and StrategyConfigMapper.use_qqq_regime_filter()
        else None
    )
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)

    full_window = {
        t: slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)
        for t in loaded
        if len(slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)) >= 22
    }
    loaded = [t for t in loaded if t in full_window]
    print(f"Loaded {len(loaded)} tickers for backtest.", flush=True)

    # -----------------------------------------------------------------------
    # Run all configs
    # -----------------------------------------------------------------------
    results = []
    for cfg in CONFIGS:
        label = cfg[0]
        desc  = cfg[-1]
        print(f"  Running {label}...", flush=True)
        combined, trades = _run_config(
            loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df,
            config_tuple=cfg,
        )
        mf   = _window_metrics(combined, FULL_START, FULL_END)
        moos = _window_metrics(combined, OOS_START,  OOS_END)
        sub_ms = [_window_metrics(combined, s, e) for _, s, e in SUB_PERIODS]
        results.append(dict(label=label, desc=desc, mf=mf, moos=moos,
                            sub_ms=sub_ms, trades=trades, series=combined))

    # -----------------------------------------------------------------------
    # Print: main comparison table
    # -----------------------------------------------------------------------
    width = 140
    sub_hdr = "  ".join(f"{lbl[:8]:>8}" for lbl, *_ in SUB_PERIODS)
    print()
    print("=" * width)
    print(
        f"PHASE 24: AGGRESSIVE vs PROD COMPARISON  "
        f"Dual {LEG_PCT:.0f}/{TOP_PCT:.0f}  {FULL_START} -> {FULL_END}"
        .center(width)
    )
    print("=" * width)
    print(
        f"  {'Config':<22}  "
        f"{'CAGR':>7}  {'Sharpe':>6}  {'MaxDD':>6}  {'C/D':>5}  {'Tr':>4}  "
        f"{'OOS CAGR':>9}  {'OOS Sh':>6}  {'OOS MDD':>7}  "
        f"sub_sharpe[{sub_hdr}]"
    )
    print("-" * width)

    for r in results:
        mf   = r["mf"]
        moos = r["moos"]
        sub_ms = r["sub_ms"]
        cd = mf["cagr"] / mf["maxdd"] if mf["maxdd"] > 0 else 99.9
        sub_str = "  ".join(f"{m['sharpe']:>8.2f}" for m in sub_ms)
        marker = "  <-- PROD" if r["label"] == "A: PROD" else ""
        print(
            f"  {r['label']:<22}  "
            f"{mf['cagr']:>+6.1f}%  {mf['sharpe']:>6.2f}  {mf['maxdd']:>5.1f}%  {cd:>5.2f}  {r['trades']:>4d}  "
            f"{moos['cagr']:>+8.1f}%  {moos['sharpe']:>6.2f}  {moos['maxdd']:>6.1f}%  "
            f"sub_sh[{sub_str}]{marker}"
        )

    # -----------------------------------------------------------------------
    # Print: delta vs PROD (Config A)
    # -----------------------------------------------------------------------
    print()
    print("DELTA vs A: PROD".center(width))
    print("-" * width)
    base = next(r for r in results if r["label"] == "A: PROD")
    for r in results:
        if r["label"] == "A: PROD":
            continue
        mf_d   = r["mf"]["cagr"]    - base["mf"]["cagr"]
        sh_d   = r["mf"]["sharpe"]  - base["mf"]["sharpe"]
        dd_d   = r["mf"]["maxdd"]   - base["mf"]["maxdd"]
        oos_d  = r["moos"]["cagr"]  - base["moos"]["cagr"]
        oossh_d = r["moos"]["sharpe"] - base["moos"]["sharpe"]
        oosdd_d = r["moos"]["maxdd"]  - base["moos"]["maxdd"]
        tr_d   = r["trades"] - base["trades"]
        sub_d  = "  ".join(
            f"{(m['sharpe'] - bm['sharpe']):>+8.2f}"
            for m, bm in zip(r["sub_ms"], base["sub_ms"])
        )
        bear_sharpe = r["sub_ms"][1]["sharpe"]  # bear2022 is index 1
        bear_flag = "  *** BEAR RISK ***" if bear_sharpe < 0 else ""
        print(
            f"  {r['label']:<22}  "
            f"DCAGR={mf_d:>+5.2f}pp  DSh={sh_d:>+5.2f}  DMD={dd_d:>+5.2f}pp  DTr={tr_d:>+4d}  "
            f"DOOS_CAGR={oos_d:>+5.2f}pp  DOOS_Sh={oossh_d:>+5.2f}  DOOS_MD={oosdd_d:>+5.2f}pp  "
            f"Dsub[{sub_d}]{bear_flag}"
        )

    # -----------------------------------------------------------------------
    # Print: sub-period detail
    # -----------------------------------------------------------------------
    print()
    print("SUB-PERIOD DETAIL (CAGR / Sharpe / MaxDD)".center(width))
    print("-" * width)
    for sub_label, sub_start, sub_end in SUB_PERIODS:
        idx = [lbl for lbl, *_ in SUB_PERIODS].index(sub_label)
        print(f"\n  [{sub_label}  {sub_start} -> {sub_end}]")
        for r in results:
            m  = r["sub_ms"][idx]
            cd = m["cagr"] / m["maxdd"] if m["maxdd"] > 0 else 99.9
            warn = ""
            if sub_label == "bear2022" and m["cagr"] < -5:
                warn = "  *** HIGH DRAWDOWN IN BEAR ***"
            print(
                f"    {r['label']:<22}  "
                f"CAGR={m['cagr']:>+6.1f}%  Sharpe={m['sharpe']:>5.2f}  "
                f"MaxDD={m['maxdd']:>5.1f}%  C/D={cd:>5.2f}{warn}"
            )

    # -----------------------------------------------------------------------
    # Print: recommendation
    # -----------------------------------------------------------------------
    prod_r   = next(r for r in results if r["label"] == "A: PROD")
    agg_r    = next(r for r in results if r["label"] == "B: Aggressive")
    semi_r   = next(r for r in results if r["label"] == "C: Semi-aggressive")

    bear_idx = [lbl for lbl, *_ in SUB_PERIODS].index("bear2022")
    agg_bear_cagr   = agg_r["sub_ms"][bear_idx]["cagr"]
    semi_bear_cagr  = semi_r["sub_ms"][bear_idx]["cagr"]
    prod_bear_cagr  = prod_r["sub_ms"][bear_idx]["cagr"]

    print()
    print("=" * width)
    print("RECOMMENDATION (한국어 요약 포함)".center(width))
    print("=" * width)

    # Determine verdict
    agg_full_cagr_gain  = agg_r["mf"]["cagr"]   - prod_r["mf"]["cagr"]
    semi_full_cagr_gain = semi_r["mf"]["cagr"]  - prod_r["mf"]["cagr"]
    agg_oos_cagr_gain   = agg_r["moos"]["cagr"]  - prod_r["moos"]["cagr"]
    semi_oos_cagr_gain  = semi_r["moos"]["cagr"] - prod_r["moos"]["cagr"]
    agg_dd_cost         = agg_r["mf"]["maxdd"]   - prod_r["mf"]["maxdd"]
    semi_dd_cost        = semi_r["mf"]["maxdd"]  - prod_r["mf"]["maxdd"]

    print(f"""
  Config A (PROD):       Full CAGR={prod_r['mf']['cagr']:>+.1f}%  OOS CAGR={prod_r['moos']['cagr']:>+.1f}%  MaxDD={prod_r['mf']['maxdd']:.1f}%  bear2022={prod_bear_cagr:>+.1f}%
  Config B (Aggressive): Full CAGR={agg_r['mf']['cagr']:>+.1f}%  OOS CAGR={agg_r['moos']['cagr']:>+.1f}%  MaxDD={agg_r['mf']['maxdd']:.1f}%  bear2022={agg_bear_cagr:>+.1f}%  (vs PROD: DCAGR={agg_full_cagr_gain:>+.2f}pp, DMD={agg_dd_cost:>+.2f}pp)
  Config C (Semi-agg):   Full CAGR={semi_r['mf']['cagr']:>+.1f}%  OOS CAGR={semi_r['moos']['cagr']:>+.1f}%  MaxDD={semi_r['mf']['maxdd']:.1f}%  bear2022={semi_bear_cagr:>+.1f}%  (vs PROD: DCAGR={semi_full_cagr_gain:>+.2f}pp, DMD={semi_dd_cost:>+.2f}pp)
""")

    # Verdict logic
    agg_dangerous = agg_bear_cagr < prod_bear_cagr - 5.0 or agg_r["mf"]["maxdd"] > prod_r["mf"]["maxdd"] + 8.0
    semi_worthwhile = semi_full_cagr_gain > 0.5 and semi_r["mf"]["maxdd"] <= prod_r["mf"]["maxdd"] + 5.0

    if agg_dangerous:
        print("  [VERDICT] Config B (Aggressive): REJECT for production deployment.")
        print(f"    - bear2022 CAGR worse by {agg_bear_cagr - prod_bear_cagr:>+.1f}pp, MaxDD larger by {agg_dd_cost:>+.1f}pp")
        print("    - Aggressive config hurts worst in bear markets, which are exactly when risk control matters most.")
    else:
        print("  [VERDICT] Config B (Aggressive): MARGINAL -- review bear2022 stats carefully before deploying.")

    if semi_worthwhile:
        print("  [VERDICT] Config C (Semi-aggressive): CONSIDER as a prod upgrade if bear2022 is acceptable.")
        print(f"    - Full CAGR gain: {semi_full_cagr_gain:>+.2f}pp  OOS CAGR gain: {semi_oos_cagr_gain:>+.2f}pp")
    else:
        print("  [VERDICT] Config C (Semi-aggressive): Insufficient improvement over prod. Stay with Config A.")

    korean_lines = [
        "",
        "  [Korean summary / \ud55c\uad6d\uc5b4 \uc694\uc57d]",
        "  -------------------------------------------------------------------------",
        "  * Config B (\uacf5\uaca9\uc801 \uc124\uc815): RSI \uae30\uc900 \ud558\ud5a5, \ubcfc\ub968 \ud544\ud130 \uc644\ud654, \ud3ec\uc9c0\uc158 \uc218 \uc99d\uac00.",
        "    \uac15\uc138\uc7a5\uc5d0\uc11c \uc218\uc775\uc774 \uc62c\ub77c\uac08 \uc218 \uc788\uc9c0\ub9cc, 2022\ub144 \uc57d\uc138\uc7a5\uc5d0\uc11c \ub099\ud3ed \ud655\ub300 \uc704\ud5d8 \uc788\uc74c.",
        "    'MAX_OPEN_POSITIONS' \uc99d\uac00 \ubc0f 'use_vol_adjusted_risk=False' (\uace0\uc815 \uc0ac\uc774\uc9d5)\ub294",
        "    \ud558\ub77d\uc7a5\uc5d0\uc11c \ud3ec\ud2b8\ud3f4\ub9ac\uc624 \uc190\uc2e4\uc744 \ubc30\uac00\uc2dc\ud0ac \uc218 \uc788\uc5b4 \ud504\ub85c\ub355\uc158 \ubc30\ud3ec\uc5d0 \uc704\ud5d8\ud568.",
        "",
        "  * Config C (\uc900\uacf5\uaca9\uc801 \uc124\uc815): \uc911\uac04 \uc218\uc900\uc758 \uc870\uc815. \ubcfc\ub968 RSI \uc18c\ud3ed \uc644\ud654 + \ud3ec\uc9c0\uc158 1\uac1c \ucd94\uac00.",
        "    \ubcc0\ub3d9\uc131 \uc2a4\ucf00\uc77c\ub9c1(vol_adjusted_risk)\uc740 \uc720\uc9c0. bear2022 \ubc29\uc5b4\ub825\uc744 \uc5b4\ub290 \uc815\ub3c4 \ubcf4\uc874.",
        "    \uc804\uccb4 CAGR \uac1c\uc120\ud3ed\uc774 \uc720\uc758\ubbf8\ud558\uace0 \ucd5c\ub300\ub099\ud3ed \uc99d\uac00\uac00 \ud5c8\uc6a9 \uc218\uc900\uc774\uba74 \uace0\ub824 \uac00\ub2a5.",
        "",
        "  * \uad8c\uc7a5\uc0ac\ud56d: bear2022 Sharpe \ubc0f MaxDD \uae30\uc900\uc744 \uba3c\uc800 \ud655\uc778\ud558\uc138\uc694.",
        "    - \uacf5\uaca9\uc801 \uc124\uc815(B)\uc774 \uc57d\uc138\uc7a5\uc5d0\uc11c -5pp \uc774\uc0c1 \uc545\ud654\ub418\uba74 PROD \uc720\uc9c0\uac00 \ucd5c\uc120.",
        "    - \uc900\uacf5\uaca9\uc801 \uc124\uc815(C)\uc774 \uc57d\uc138\uc7a5 \ub099\ud3ed\uc744 \ud06c\uac8c \ub298\ub9ac\uc9c0 \uc54a\uc73c\uba74\uc11c CAGR +1pp \uc774\uc0c1\uc774\uba74 \uac80\ud1a0 \uac00\uce58 \uc788\uc74c.",
        "  -------------------------------------------------------------------------",
        "",
    ]
    for line in korean_lines:
        print(line)

    print("=" * width)
    best = max(results, key=lambda r: r["mf"]["cagr"] / max(r["mf"]["maxdd"], 0.01))
    print(f"  Best MAR (full period): '{best['label']}' = {best['mf']['cagr'] / max(best['mf']['maxdd'], 0.01):.3f}")
    best_oos = max(results, key=lambda r: r["moos"]["cagr"] / max(r["moos"]["maxdd"], 0.01))
    print(f"  Best OOS MAR:           '{best_oos['label']}' = {best_oos['moos']['cagr'] / max(best_oos['moos']['maxdd'], 0.01):.3f}")
    print("=" * width)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
