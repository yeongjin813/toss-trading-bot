"""Tests for execution friction, VIX regime, and entry confirmation."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from analytics import (
    BarSnapshot,
    LiveSignalEngine,
    PositionState,
    build_vix_regime_lookup,
    resolve_vix_allows_buys,
)
from config import StrategyConfigMapper
from execution_friction import fill_price, round_trip_cost_pct, slippage_fraction
from trading_features import apply_vix_regime_gate, resolve_bar_regime


class ExecutionFrictionTests(unittest.TestCase):
    def test_slippage_buy_worse_sell_better(self) -> None:
        ref = 100.0
        bps = 10.0
        self.assertGreater(fill_price("BUY", ref, slippage_bps=bps), ref)
        self.assertLess(fill_price("SELL", ref, slippage_bps=bps), ref)

    def test_round_trip_cost_includes_commission_and_slippage(self) -> None:
        cost = round_trip_cost_pct(0.001, 10.0)
        self.assertAlmostEqual(
            cost,
            2.0 * (0.001 + slippage_fraction(10.0)) * 100.0,
        )


class VixRegimeTests(unittest.TestCase):
    def test_build_lookup_blocks_high_vix(self) -> None:
        frame = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "Close": [20.0, 30.0],
            }
        )
        lookup = build_vix_regime_lookup(frame, max_vix=25.0)
        self.assertTrue(resolve_vix_allows_buys(lookup, "2024-01-02"))
        self.assertFalse(resolve_vix_allows_buys(lookup, "2024-01-03"))

    def test_resolve_bar_regime_vix_overlay(self) -> None:
        spy_lookup = {"2024-01-02": True}
        vix_lookup = {"2024-01-02": False}
        with patch.object(StrategyConfigMapper, "use_vix_regime_filter", return_value=True):
            regime = resolve_bar_regime(
                "2024-01-02",
                regime_lookup=None,
                spy_lookup=spy_lookup,
                vix_lookup=vix_lookup,
            )
        self.assertFalse(regime.allow_new_buys)
        self.assertEqual(regime.label, "vix_elevated")


class EntryConfirmationTests(unittest.TestCase):
    def test_entry_requires_consecutive_days(self) -> None:
        engine = LiveSignalEngine("TEST")
        state = PositionState()
        bars = [
            (
                BarSnapshot(
                    date=pd.Timestamp("2024-06-03"),
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    volume=1_000_000,
                    sma_short=99.0,
                    sma_long=95.0,
                    rsi=45.0,
                    atr=2.0,
                    volume_sma=500_000,
                ),
                BarSnapshot(
                    date=pd.Timestamp("2024-06-02"),
                    open=98.0,
                    high=99.0,
                    low=97.0,
                    close=98.5,
                    volume=900_000,
                    sma_short=98.0,
                    sma_long=94.0,
                    rsi=40.0,
                    atr=2.0,
                    volume_sma=500_000,
                ),
            ),
            (
                BarSnapshot(
                    date=pd.Timestamp("2024-06-04"),
                    open=100.5,
                    high=102.0,
                    low=100.0,
                    close=101.0,
                    volume=1_100_000,
                    sma_short=99.5,
                    sma_long=95.5,
                    rsi=46.0,
                    atr=2.0,
                    volume_sma=500_000,
                ),
                BarSnapshot(
                    date=pd.Timestamp("2024-06-03"),
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    volume=1_000_000,
                    sma_short=99.0,
                    sma_long=95.0,
                    rsi=45.0,
                    atr=2.0,
                    volume_sma=500_000,
                ),
            ),
        ]

        with patch.object(engine, "_entry_signal", return_value=True):
            with patch.object(
                engine, "_entry_setup_still_valid", return_value=True
            ):
                with patch.object(
                    StrategyConfigMapper, "entry_confirmation_days", return_value=2
                ):
                    first = engine.evaluate_bar(
                        state,
                        bars[0][0],
                        bars[0][1],
                        mutate_state=True,
                        market_bullish=True,
                    )
                    second = engine.evaluate_bar(
                        state,
                        bars[1][0],
                        bars[1][1],
                        mutate_state=True,
                        market_bullish=True,
                    )

        self.assertEqual(first["signal"], "HOLD")
        self.assertTrue(first.get("entry_confirm_pending"))
        self.assertEqual(second["signal"], "BUY")


if __name__ == "__main__":
    unittest.main()
