"""Tests for enhanced momentum selection factors."""

from __future__ import annotations

import pandas as pd

from momentum_ranker import MomentumRankSettings, rank_universe_frames
from momentum_selection import (
    build_composite_ranking,
    frog_in_the_pan_score,
    inverse_volatility_weights,
    rolling_return_skewness,
    skewness_penalty,
    target_set_unchanged,
)


def _frame(daily_return: float, bars: int = 280) -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-02", periods=bars)
    closes = [100.0]
    for _ in range(bars - 1):
        closes.append(closes[-1] * (1.0 + daily_return))
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1_000_000] * bars,
        }
    )


def test_fip_prefers_smooth_uptrend() -> None:
    smooth = pd.Series([0.001] * 252)
    choppy = pd.Series([0.01 if i % 2 == 0 else -0.009 for i in range(252)])
    fip_smooth = frog_in_the_pan_score(smooth, window=252)
    fip_choppy = frog_in_the_pan_score(choppy, window=252)
    assert fip_smooth is not None and fip_choppy is not None
    assert fip_smooth > fip_choppy


def test_skewness_penalty_only_negative() -> None:
    assert skewness_penalty(0.5) == 0.0
    assert skewness_penalty(-1.0) > 0.0


def test_inverse_vol_weights_sum_to_one() -> None:
    weights = inverse_volatility_weights({"AAA": 0.02, "BBB": 0.04, "CCC": 0.01})
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert weights["CCC"] > weights["AAA"] > weights["BBB"]


def test_target_set_unchanged_order_insensitive() -> None:
    assert target_set_unchanged(["A", "B", "C"], ["C", "B", "A"]) is True
    assert target_set_unchanged(["A", "B"], ["A", "C"]) is False


def test_enhanced_ranking_prefers_strong_momentum() -> None:
    frames = {
        "AAA": _frame(0.002, bars=300),
        "BBB": _frame(0.0005, bars=300),
        "CCC": _frame(-0.001, bars=300),
    }
    settings = MomentumRankSettings(
        enabled=True,
        top_n=2,
        ranking_mode="enhanced",
        require_above_sma50=False,
        require_above_sma200=False,
        require_near_52w_high=False,
        min_bars=252,
    )
    ranked = rank_universe_frames(frames, list(frames), settings=settings)
    assert len(ranked) == 3
    order = [row.ticker for row in ranked]
    assert order.index("AAA") < order.index("CCC")


def test_build_composite_ranking_orders_by_score() -> None:
    raw = {
        "AAA": {
            "ret_60": 0.10,
            "ret_120": 0.12,
            "ret_252": 0.15,
            "fip_score": 0.7,
            "skewness_90d": -0.2,
            "skewness_penalty": skewness_penalty(-0.2),
            "vol_126d": 0.02,
            "close": 100.0,
            "above_sma50": True,
            "above_sma200": True,
        },
        "BBB": {
            "ret_60": 0.02,
            "ret_120": 0.03,
            "ret_252": 0.04,
            "fip_score": 0.5,
            "skewness_90d": -1.0,
            "skewness_penalty": skewness_penalty(-1.0),
            "vol_126d": 0.03,
            "close": 90.0,
            "above_sma50": True,
            "above_sma200": True,
        },
    }
    ranked = build_composite_ranking(raw)
    assert ranked[0].ticker == "AAA"


def main() -> int:
    test_fip_prefers_smooth_uptrend()
    test_skewness_penalty_only_negative()
    test_inverse_vol_weights_sum_to_one()
    test_target_set_unchanged_order_insensitive()
    test_enhanced_ranking_prefers_strong_momentum()
    test_build_composite_ranking_orders_by_score()
    print("ALL MOMENTUM SELECTION TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
