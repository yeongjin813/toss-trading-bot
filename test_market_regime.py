"""Tests for SPY/QQQ market regime with golden cross confirmation."""

from __future__ import annotations

import pandas as pd

from analytics import build_market_regime_lookup, volatility_adjusted_risk_fraction


def _trend_frame(start: float, daily_drift: float, days: int = 260) -> pd.DataFrame:
    closes = [start]
    for _ in range(days - 1):
        closes.append(closes[-1] * (1.0 + daily_drift))
    dates = pd.date_range("2024-01-01", periods=days, freq="B")
    return pd.DataFrame(
        {
            "Date": dates.strftime("%Y-%m-%d"),
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1_000_000] * days,
        }
    )


def test_golden_cross_downgrades_to_cautious() -> None:
    spy = _trend_frame(500.0, -0.002, days=260)
    qqq = _trend_frame(400.0, 0.001, days=260)
    lookup = build_market_regime_lookup(
        spy,
        qqq,
        require_golden_cross=True,
    )
    last_date = spy["Date"].iloc[-1]
    regime = lookup[last_date]
    assert regime.label == "cautious"
    assert regime.position_size_multiplier == 0.5
    assert regime.max_open_positions == 2


def test_volatility_adjusted_risk_scales_down() -> None:
    high_vol = volatility_adjusted_risk_fraction(0.01, 0.03, target_vol_pct=0.015)
    low_vol = volatility_adjusted_risk_fraction(0.01, 0.01, target_vol_pct=0.015)
    assert high_vol < 0.01
    assert low_vol == 0.01


def main() -> int:
    test_golden_cross_downgrades_to_cautious()
    test_volatility_adjusted_risk_scales_down()
    print("ALL REGIME TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
