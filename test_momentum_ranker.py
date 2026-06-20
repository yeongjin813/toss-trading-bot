"""Tests for cross-sectional momentum ranker."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytz

from momentum_ranker import (
    MomentumRankSettings,
    build_cycle_tickers,
    is_new_buy_allowed,
    rank_universe_frames,
    rebalance_active_tickers,
    should_rebalance_today,
)


def _synthetic_frame(
    ticker: str,
    *,
    daily_return: float,
    start_price: float = 100.0,
    bars: int = 280,
) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=bars)
    closes = [start_price]
    for _ in range(bars - 1):
        closes.append(closes[-1] * (1.0 + daily_return))
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1_000_000 + i * 100 for i in range(bars)],
        }
    )


class _FakeCache:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self._frames = frames

    def get_frame(self, ticker: str) -> pd.DataFrame:
        return self._frames[ticker]


def test_rank_universe_prefers_strong_momentum() -> None:
    frames = {
        "AAA": _synthetic_frame("AAA", daily_return=0.002),
        "BBB": _synthetic_frame("BBB", daily_return=0.0005),
        "CCC": _synthetic_frame("CCC", daily_return=-0.001),
    }
    settings = MomentumRankSettings(
        enabled=True,
        top_n=2,
        require_above_sma50=False,
        require_above_sma200=False,
        min_bars=200,
    )
    ranked = rank_universe_frames(frames, list(frames), settings=settings)
    assert ranked[0].ticker == "AAA"
    assert ranked[-1].ticker == "CCC"


def test_build_cycle_tickers_keeps_held_names() -> None:
    universe = ["AAA", "BBB", "CCC"]
    states = {
        "BBB": {"held_quantity": 2, "pending_order": False},
        "_portfolio": {"active_trade_tickers": ["AAA"]},
    }
    cycle = build_cycle_tickers(universe, ["AAA"], states)
    assert cycle == ["AAA", "BBB"]


def test_buy_gate_blocks_outside_active_set() -> None:
    settings = MomentumRankSettings(enabled=True, top_n=3)
    active = frozenset({"NVDA", "META"})
    assert is_new_buy_allowed("NVDA", active, settings=settings) is True
    assert is_new_buy_allowed("AAPL", active, settings=settings) is False
    assert is_new_buy_allowed("AAPL", active, settings=MomentumRankSettings(enabled=False)) is True


def test_rebalance_runs_on_friday() -> None:
    ny = pytz.timezone("America/New_York")
    friday = ny.localize(datetime(2026, 6, 19, 10, 0, 0))
    assert should_rebalance_today(friday, None, rebalance_weekday=4) is True
    assert should_rebalance_today(friday, "2026-06-19", rebalance_weekday=4) is False


def test_rebalance_persists_active_tickers() -> None:
    frames = {
        "AAA": _synthetic_frame("AAA", daily_return=0.002),
        "BBB": _synthetic_frame("BBB", daily_return=0.001),
        "CCC": _synthetic_frame("CCC", daily_return=0.0002),
    }
    cache = _FakeCache(frames)
    states: dict = {}
    settings = MomentumRankSettings(
        enabled=True,
        top_n=2,
        require_above_sma50=False,
        require_above_sma200=False,
        min_bars=200,
    )
    snapshot = rebalance_active_tickers(
        cache,
        list(frames),
        states,
        settings=settings,
        force=True,
    )
    assert len(snapshot.active_tickers) == 2
    assert states["_portfolio"]["active_trade_tickers"] == snapshot.active_tickers


def test_enhanced_mode_uses_composite_fields() -> None:
    frames = {
        "AAA": _synthetic_frame("AAA", daily_return=0.002, bars=300),
        "BBB": _synthetic_frame("BBB", daily_return=0.001, bars=300),
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
    assert ranked
    assert ranked[0].momentum_subscore >= 0
    assert ranked[0].vol_126d > 0


def test_for_production_forces_legacy() -> None:
    settings = MomentumRankSettings.from_env()
    settings = MomentumRankSettings(
        ranking_mode="enhanced",
        dynamic_rebalance_only=True,
        inverse_vol_weighting=True,
    )
    prod = settings.for_production()
    assert prod.ranking_mode == "legacy"
    assert prod.dynamic_rebalance_only is False
    assert prod.inverse_vol_weighting is False


def main() -> int:
    test_rank_universe_prefers_strong_momentum()
    test_build_cycle_tickers_keeps_held_names()
    test_buy_gate_blocks_outside_active_set()
    test_rebalance_runs_on_friday()
    test_rebalance_persists_active_tickers()
    test_enhanced_mode_uses_composite_fields()
    test_for_production_forces_legacy()
    print("ALL MOMENTUM RANKER TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
