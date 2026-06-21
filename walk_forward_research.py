"""
Rolling out-of-sample walk-forward helpers for Top3 momentum research.

Train window: pick best allocation/ranking variant by Sharpe.
Test window: report OOS metrics (no peeking).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from momentum_ranker import MomentumRankSettings
from top3_backtest import analytics_for_top3_result, run_top3_backtest

# (train_label, train_start, train_end, test_label, test_start, test_end)
ROLLING_OOS_FOLDS: list[tuple[str, str, str, str, str, str]] = [
    ("2017-2019", "2017-01-01", "2019-12-31", "2020", "2020-01-01", "2020-12-31"),
    ("2018-2020", "2018-01-01", "2020-12-31", "2021", "2021-01-01", "2021-12-31"),
    ("2019-2021", "2019-01-01", "2021-12-31", "2022", "2022-01-01", "2022-12-31"),
    ("2020-2022", "2020-01-01", "2022-12-31", "2023", "2023-01-01", "2023-12-31"),
    ("2021-2023", "2021-01-01", "2023-12-31", "2024", "2024-01-01", "2024-12-31"),
    ("2022-2024", "2022-01-01", "2024-12-31", "2025", "2025-01-01", "2025-12-31"),
]

def _base_top3_settings() -> MomentumRankSettings:
    base = MomentumRankSettings.from_env()
    return replace(
        base,
        enabled=True,
        min_bars=min(base.min_bars, 60),
    )


def top3_variant_configs() -> dict[str, MomentumRankSettings]:
    """Named Top3 research configs (legacy equal remains production default)."""
    base = _base_top3_settings()
    return {
        "legacy_equal": replace(
            base,
            ranking_mode="legacy",
            dynamic_rebalance_only=False,
            inverse_vol_weighting=False,
        ),
        "legacy_invvol": replace(
            base,
            ranking_mode="legacy",
            dynamic_rebalance_only=False,
            inverse_vol_weighting=True,
        ),
        "enhanced": replace(
            base,
            ranking_mode="enhanced",
            dynamic_rebalance_only=True,
            inverse_vol_weighting=True,
        ),
    }


def run_top3_window(
    tickers: list[str],
    ohlcv_by_ticker: Mapping[str, Any],
    *,
    settings: MomentumRankSettings,
    initial_cash: float,
    commission_rate: float,
    slippage_bps: float,
    window_start: str,
    window_end: str,
) -> dict[str, float | int]:
    result = run_top3_backtest(
        tickers=tickers,
        ohlcv_by_ticker=dict(ohlcv_by_ticker),
        initial_cash=initial_cash,
        commission_rate=commission_rate,
        slippage_bps=slippage_bps,
        momentum_settings=settings,
        window_start=window_start,
        window_end=window_end,
    )
    return analytics_for_top3_result(result)


def pick_train_winner(
    train_metrics: dict[str, dict[str, float | int]],
) -> str:
    """Select variant with highest train Sharpe (tie-break: higher CAGR)."""
    best_name = "legacy_equal"
    best_sharpe = float("-inf")
    best_cagr = float("-inf")
    for name, metrics in train_metrics.items():
        sharpe = float(metrics["sharpe"])
        cagr = float(metrics["cagr_pct"])
        if sharpe > best_sharpe or (sharpe == best_sharpe and cagr > best_cagr):
            best_name = name
            best_sharpe = sharpe
            best_cagr = cagr
    return best_name


def run_rolling_oos_fold(
    tickers: list[str],
    ohlcv_by_ticker: Mapping[str, Any],
    fold: tuple[str, str, str, str, str, str],
    *,
    initial_cash: float = 10_000.0,
    commission_rate: float = 0.001,
    slippage_bps: float = 5.0,
) -> dict[str, Any]:
    train_label, train_start, train_end, test_label, test_start, test_end = fold
    variants = top3_variant_configs()
    train_metrics: dict[str, dict[str, float | int]] = {}
    test_metrics: dict[str, dict[str, float | int]] = {}

    for name, cfg in variants.items():
        train_metrics[name] = run_top3_window(
            tickers,
            ohlcv_by_ticker,
            settings=cfg,
            initial_cash=initial_cash,
            commission_rate=commission_rate,
            slippage_bps=slippage_bps,
            window_start=train_start,
            window_end=train_end,
        )
        test_metrics[name] = run_top3_window(
            tickers,
            ohlcv_by_ticker,
            settings=cfg,
            initial_cash=initial_cash,
            commission_rate=commission_rate,
            slippage_bps=slippage_bps,
            window_start=test_start,
            window_end=test_end,
        )

    winner = pick_train_winner(train_metrics)
    baseline = "legacy_equal"
    return {
        "train_label": train_label,
        "test_label": test_label,
        "winner": winner,
        "train_metrics": train_metrics,
        "test_metrics": test_metrics,
        "winner_test_sharpe": float(test_metrics[winner]["sharpe"]),
        "winner_test_cagr": float(test_metrics[winner]["cagr_pct"]),
        "baseline_test_sharpe": float(test_metrics[baseline]["sharpe"]),
        "baseline_test_cagr": float(test_metrics[baseline]["cagr_pct"]),
    }


def print_oos_summary_table(rows: list[dict[str, Any]]) -> None:
    width = 96
    print("=" * width)
    print("ROLLING OOS WALK-FORWARD (Top3 momentum variants)".center(width))
    print("=" * width)
    print(
        f"{'Train':<12} {'Test':<6} {'Winner':<14} "
        f"{'Win OOS Sharpe':>14} {'Win OOS CAGR%':>14} "
        f"{'Base Sharpe':>12} {'Base CAGR%':>12}"
    )
    print("-" * width)
    for row in rows:
        print(
            f"{row['train_label']:<12} {row['test_label']:<6} {row['winner']:<14} "
            f"{row['winner_test_sharpe']:>14.2f} {row['winner_test_cagr']:>+14.1f} "
            f"{row['baseline_test_sharpe']:>12.2f} {row['baseline_test_cagr']:>+12.1f}"
        )
    if rows:
        avg_win = sum(r["winner_test_sharpe"] for r in rows) / len(rows)
        avg_base = sum(r["baseline_test_sharpe"] for r in rows) / len(rows)
        print("-" * width)
        print(
            f"{'MEAN OOS':<12} {'':<6} {'':<14} "
            f"{avg_win:>14.2f} {'':<14} "
            f"{avg_base:>12.2f}"
        )
    print("=" * width)
    print(
        "Baseline = legacy_equal (production). Winner = highest train Sharpe per fold."
    )
