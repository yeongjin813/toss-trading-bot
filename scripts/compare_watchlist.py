"""Compare dual backtest: 15 tech-only vs 25 diversified watchlist."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from backtest_benchmarks import summarize_strategy_vs_benchmarks
from config import StrategyConfigMapper
from deployment_config import DeploymentConfig, scaled_capital
from market_registry import BENCHMARK_TICKER, SECONDARY_BENCHMARK_TICKER
from momentum_ranker import MomentumRankSettings
from portfolio_backtest import run_portfolio_backtest
from run_backtest import (
    WALK_FORWARD_WINDOWS,
    YFINANCE_WARMUP_START,
    default_capital_at_risk,
    fetch_yfinance_ohlcv,
    load_yfinance_watchlist_data,
    slice_ohlcv_window,
)
from top3_backtest import run_top3_backtest, summarize_dual_combined

OLD_TECH = [
    "AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "TSLA", "AMD", "AVGO",
    "NFLX", "PLTR", "CRWD", "TSM", "SHOP", "UBER",
]
NEW_DIV = OLD_TECH + ["LLY", "UNH", "JNJ", "JPM", "V", "XOM", "COST", "WMT", "KO", "CAT"]


def _momentum_settings() -> tuple[MomentumRankSettings, MomentumRankSettings]:
    mom = MomentumRankSettings.from_env()
    legacy_mom = MomentumRankSettings(
        enabled=mom.enabled,
        top_n=mom.top_n,
        rebalance_weekday=mom.rebalance_weekday,
        weight_3m=mom.weight_3m,
        weight_6m=mom.weight_6m,
        weight_12m=mom.weight_12m,
        weight_volume=mom.weight_volume,
        require_above_sma50=mom.require_above_sma50,
        require_above_sma200=mom.require_above_sma200,
        require_near_52w_high=mom.require_near_52w_high,
        near_52w_high_pct=mom.near_52w_high_pct,
        sector_diversify=mom.sector_diversify,
        max_per_sector=mom.max_per_sector,
        min_bars=mom.min_bars,
    )
    top3_mom = MomentumRankSettings(
        enabled=True,
        top_n=mom.top_n,
        rebalance_weekday=mom.rebalance_weekday,
        weight_3m=mom.weight_3m,
        weight_6m=mom.weight_6m,
        weight_12m=mom.weight_12m,
        weight_volume=mom.weight_volume,
        require_above_sma50=mom.require_above_sma50,
        require_above_sma200=mom.require_above_sma200,
        require_near_52w_high=mom.require_near_52w_high,
        near_52w_high_pct=mom.near_52w_high_pct,
        sector_diversify=mom.sector_diversify,
        max_per_sector=mom.max_per_sector,
        min_bars=min(mom.min_bars, 60),
    )
    return legacy_mom, top3_mom


def run_dual(
    label: str,
    tickers: list[str],
    *,
    leg_cash: float,
    top_cash: float,
    leg_pct: float,
    top_pct: float,
    legacy_mom: MomentumRankSettings,
    top3_mom: MomentumRankSettings,
    full_cache: dict[str, dict],
    spy_df,
    qqq_df,
    use_spy: bool,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    cache_key = ",".join(tickers)
    if cache_key not in full_cache:
        full, skipped = load_yfinance_watchlist_data(tickers, start=YFINANCE_WARMUP_START)
        full_cache[cache_key] = {"full": full, "skipped": skipped}
    else:
        full = full_cache[cache_key]["full"]
        skipped = full_cache[cache_key]["skipped"]

    loaded = [t for t in tickers if t in full]
    if start is None and end is None:
        ohlcv = {t: full[t].copy() for t in loaded}
    else:
        ohlcv = {
            t: slice_ohlcv_window(full[t], start, end)
            for t in loaded
        }
        ohlcv = {t: df for t, df in ohlcv.items() if len(df) >= 22}
        loaded = list(ohlcv.keys())

    legacy = run_portfolio_backtest(
        tickers=loaded,
        ohlcv_by_ticker=ohlcv,
        initial_cash=leg_cash,
        use_spy_market_filter=use_spy,
        spy_df=spy_df,
        qqq_df=qqq_df,
        momentum_settings=legacy_mom,
    )
    top3 = run_top3_backtest(
        tickers=loaded,
        ohlcv_by_ticker=full,
        initial_cash=top_cash,
        momentum_settings=top3_mom,
        window_start=start,
        window_end=end,
    )
    comb = summarize_dual_combined(legacy, top3, legacy_pct=leg_pct, top3_pct=top_pct)
    bench = summarize_strategy_vs_benchmarks(
        comb["total_return_pct"],
        ohlcv,
        spy_df=spy_df,
        window_start=start,
        window_end=end,
    )
    return {
        "label": label,
        "n": len(loaded),
        "skipped": skipped,
        "legacy_ret": legacy.total_return_pct,
        "top3_ret": top3.total_return_pct,
        "comb_ret": comb["total_return_pct"],
        "comb_mdd": comb["max_drawdown_pct"],
        "comb_sharpe": comb["sharpe_ratio"],
        "comb_trades": comb["total_trades"],
        "spy_ret": bench.get("spy_return_pct"),
        "alpha_spy": bench.get("alpha_vs_spy_pct"),
    }


def main() -> int:
    cash = default_capital_at_risk()
    deploy = DeploymentConfig.from_env()
    if deploy.is_dual:
        leg_pct, top_pct = deploy.legacy_capital_pct, deploy.top3_capital_pct
    else:
        leg_pct, top_pct = 60.0, 40.0
    leg_cash = scaled_capital(cash, leg_pct / (leg_pct + top_pct))
    top_cash = scaled_capital(cash, top_pct / (leg_pct + top_pct))
    legacy_mom, top3_mom = _momentum_settings()

    use_spy = StrategyConfigMapper.use_spy_market_filter()
    use_qqq = StrategyConfigMapper.use_qqq_regime_filter()
    spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START) if use_spy else None
    qqq_df = (
        fetch_yfinance_ohlcv(SECONDARY_BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
        if use_qqq
        else None
    )

    print(f"Capital: ${cash:,.0f}  split {leg_pct:.0f}/{top_pct:.0f}  (yfinance from {YFINANCE_WARMUP_START})")
    print()

    full_cache: dict[str, dict] = {}
    rows: list[dict] = []

    rows.append(
        run_dual(
            "FULL | 15 tech",
            OLD_TECH,
            leg_cash=leg_cash,
            top_cash=top_cash,
            leg_pct=leg_pct,
            top_pct=top_pct,
            legacy_mom=legacy_mom,
            top3_mom=top3_mom,
            full_cache=full_cache,
            spy_df=spy_df,
            qqq_df=qqq_df,
            use_spy=use_spy,
        )
    )
    rows.append(
        run_dual(
            "FULL | 25 diversified",
            NEW_DIV,
            leg_cash=leg_cash,
            top_cash=top_cash,
            leg_pct=leg_pct,
            top_pct=top_pct,
            legacy_mom=legacy_mom,
            top3_mom=top3_mom,
            full_cache=full_cache,
            spy_df=spy_df,
            qqq_df=qqq_df,
            use_spy=use_spy,
        )
    )

    for wf_label, start, end in WALK_FORWARD_WINDOWS:
        if wf_label not in {"2022-2024", "2024-2026"}:
            continue
        rows.append(
            run_dual(
                f"{wf_label} | 15 tech",
                OLD_TECH,
                leg_cash=leg_cash,
                top_cash=top_cash,
                leg_pct=leg_pct,
                top_pct=top_pct,
                legacy_mom=legacy_mom,
                top3_mom=top3_mom,
                full_cache=full_cache,
                spy_df=spy_df,
                qqq_df=qqq_df,
                use_spy=use_spy,
                start=start,
                end=end,
            )
        )
        rows.append(
            run_dual(
                f"{wf_label} | 25 diversified",
                NEW_DIV,
                leg_cash=leg_cash,
                top_cash=top_cash,
                leg_pct=leg_pct,
                top_pct=top_pct,
                legacy_mom=legacy_mom,
                top3_mom=top3_mom,
                full_cache=full_cache,
                spy_df=spy_df,
                qqq_df=qqq_df,
                use_spy=use_spy,
                start=start,
                end=end,
            )
        )

    width = 96
    print("=" * width)
    header = (
        f"{'Scenario':<30} {'N':>3} {'Return':>9} {'MaxDD':>8} "
        f"{'Sharpe':>7} {'SPY':>8} {'Alpha':>8} {'Trades':>7}"
    )
    print(header)
    print("-" * width)
    for row in rows:
        spy = row["spy_ret"]
        alpha = row["alpha_spy"]
        print(
            f"{row['label']:<30} {row['n']:>3} {row['comb_ret']:>+8.1f}% "
            f"{row['comb_mdd']:>7.1f}% {row['comb_sharpe']:>7.2f} "
            f"{spy if spy is not None else 0:>+7.1f}% "
            f"{alpha if alpha is not None else 0:>+7.1f}% {row['comb_trades']:>7}"
        )
    print("=" * width)

    for row in rows[:2]:
        if row["skipped"]:
            print(f"{row['label']} skipped: {row['skipped']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
