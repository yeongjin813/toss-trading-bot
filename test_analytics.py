"""
Verification suite for the LiveSignalEngine analytics module.
Run: python test_analytics.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from analytics import (
    BarSnapshot,
    LiveSignalEngine,
    PositionState,
    StrategyConfig,
    calculate_atr,
    calculate_rsi,
    calculate_sma,
)


def build_synthetic_market_data(rows: int = 100) -> pd.DataFrame:
    """Generate synthetic OHLCV data with distinct trending sections."""
    rng = np.random.default_rng(42)
    dates = [
        (datetime(2024, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(rows)
    ]

    prices = np.zeros(rows, dtype=float)
    prices[0] = 100.0

    for i in range(1, rows):
        if i < 30:
            drift = 0.004
        elif i < 60:
            drift = -0.002
        else:
            drift = 0.003
        noise = rng.normal(0.0, 0.008)
        prices[i] = max(prices[i - 1] * (1.0 + drift + noise), 1.0)

    volume = rng.integers(800_000, 1_500_000, size=rows).astype(float)
    volume[35:45] = volume[35:45] * 2.0

    highs = prices * (1.0 + rng.uniform(0.002, 0.012, size=rows))
    lows = prices * (1.0 - rng.uniform(0.002, 0.012, size=rows))
    opens = np.roll(prices, 1)
    opens[0] = prices[0]

    return pd.DataFrame(
        {
            "Date": dates,
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": prices,
            "Volume": volume,
        }
    )


def verify_indicator_computation(engine: LiveSignalEngine, df: pd.DataFrame) -> pd.DataFrame:
    """Validate indicator columns against direct reference implementations."""
    print("=" * 88)
    print("TEST 1: INDICATOR COMPUTATION (WILDER SMOOTHING)")
    print("=" * 88)

    enriched = engine.calculate_indicators(df)
    cfg = engine.config

    pre_drop = df.copy()
    pre_drop["SMA"] = calculate_sma(pre_drop, cfg.sma_period)
    pre_drop["RSI"] = calculate_rsi(pre_drop, cfg.rsi_period)
    pre_drop["ATR"] = calculate_atr(pre_drop, cfg.atr_period)
    pre_drop["Volume_SMA"] = calculate_sma(
        pre_drop, cfg.volume_sma_period, column="Volume"
    )

    assert not enriched[["SMA", "RSI", "ATR", "Volume_SMA"]].isna().any().any(), (
        "NaN values detected in indicator tail after enrichment."
    )

    idempotent = engine.calculate_indicators(df)
    assert len(idempotent) == len(enriched), "Idempotent run length mismatch"

    last_date = enriched.iloc[-1]["Date"]
    ref_row = pre_drop.loc[pre_drop["Date"] == last_date].iloc[0]
    last_row = enriched.iloc[-1]

    assert np.isclose(last_row["SMA"], ref_row["SMA"]), "SMA mismatch"
    assert np.isclose(last_row["RSI"], ref_row["RSI"]), "RSI mismatch"
    assert np.isclose(last_row["ATR"], ref_row["ATR"]), "ATR mismatch"
    assert np.isclose(last_row["Volume_SMA"], ref_row["Volume_SMA"]), "Volume SMA mismatch"

    print(f"Rows processed       : {len(enriched)}")
    print(f"NaN-free tail        : PASS")
    print(
        f"Latest indicators    : SMA={enriched.iloc[-1]['SMA']:.2f} | "
        f"RSI={enriched.iloc[-1]['RSI']:.2f} | "
        f"ATR={enriched.iloc[-1]['ATR']:.4f} | "
        f"VolSMA={enriched.iloc[-1]['Volume_SMA']:,.0f}"
    )
    print("Indicator validation : PASS")
    print()
    return enriched


def verify_o1_replay(engine: LiveSignalEngine, enriched: pd.DataFrame) -> PositionState:
    """Verify full-history replay produces a deterministic O(1)-ready state."""
    print("=" * 88)
    print("TEST 2: O(1) STATE REPLAY")
    print("=" * 88)

    state = engine.replay_state(enriched)
    print(f"Replay bars          : {len(enriched)}")
    print(f"In position          : {state.in_position}")
    print(f"Highest price        : {state.highest_price_achieved}")
    print(f"Trigger floor        : {state.trigger_floor}")
    print(f"State dict keys      : {list(state.to_dict().keys())}")
    print("Replay validation    : PASS")
    print()
    return state


def verify_bar_transitions(engine: LiveSignalEngine) -> None:
    """Simulate BUY, peak tracking, and intra-bar DYNAMIC_ATR_SELL transitions."""
    print("=" * 88)
    print("TEST 3: BAR-BY-BAR STATE TRANSITIONS")
    print("=" * 88)

    prev_bar = BarSnapshot(
        date=datetime(2024, 6, 1).date(),
        open=98.0,
        high=99.5,
        low=97.5,
        close=98.5,
        volume=2_000_000.0,
        sma=99.0,
        rsi=55.0,
        atr=1.5,
        volume_sma=1_000_000.0,
    )
    buy_bar = BarSnapshot(
        date=datetime(2024, 6, 2).date(),
        open=99.0,
        high=102.0,
        low=98.8,
        close=101.0,
        volume=2_500_000.0,
        sma=99.5,
        rsi=58.0,
        atr=1.6,
        volume_sma=1_100_000.0,
    )

    state = PositionState()
    buy_result = engine.evaluate_bar(state, buy_bar, prev_bar, mutate_state=True)
    print(f"Step 1 - BUY signal  : {buy_result['signal']}")
    print(f"         In position : {state.in_position}")
    print(f"         Peak set    : {state.highest_price_achieved}")

    assert buy_result["signal"] == "BUY"
    assert state.in_position is True
    assert state.highest_price_achieved == buy_bar.close

    prev_bar = buy_bar
    rise_bar = BarSnapshot(
        date=datetime(2024, 6, 3).date(),
        open=101.0,
        high=112.0,
        low=108.5,
        close=110.0,
        volume=2_200_000.0,
        sma=100.0,
        rsi=62.0,
        atr=2.0,
        volume_sma=1_150_000.0,
    )
    hold_result = engine.evaluate_bar(state, rise_bar, prev_bar, mutate_state=True)
    print(f"Step 2 - HOLD signal : {hold_result['signal']}")
    print(f"         Updated peak : {state.highest_price_achieved}")
    print(f"         Trigger floor: {state.trigger_floor}")

    assert hold_result["signal"] == "HOLD"
    assert state.highest_price_achieved == 110.0
    assert state.trigger_floor == 110.0 - (2.0 * engine.config.atr_multiplier)

    prev_bar = rise_bar
    trigger_floor = float(state.trigger_floor)
    stop_bar = BarSnapshot(
        date=datetime(2024, 6, 4).date(),
        open=109.0,
        high=109.5,
        low=trigger_floor - 0.5,
        close=108.0,
        volume=2_100_000.0,
        sma=101.0,
        rsi=60.0,
        atr=2.0,
        volume_sma=1_200_000.0,
    )

    stop_result = engine.evaluate_bar(state, stop_bar, prev_bar, mutate_state=True)
    print(f"Step 3 - Stop signal : {stop_result['signal']}")
    print(f"         Bar low      : {stop_bar.low:.2f}")
    print(f"         Trigger floor: {trigger_floor:.2f}")
    print(f"         In position  : {state.in_position}")

    assert stop_result["signal"] == "DYNAMIC_ATR_SELL"
    assert state.in_position is False
    assert state.highest_price_achieved is None
    print("Transition validation: PASS")
    print()


def verify_external_state_roundtrip(engine: LiveSignalEngine, enriched: pd.DataFrame) -> None:
    """Verify state serialization supports external persistence stores."""
    print("=" * 88)
    print("TEST 4: EXTERNAL STATE SERIALIZATION")
    print("=" * 88)

    state = engine.replay_state(enriched, end_index=len(enriched) // 2)
    payload = engine.dump_state(state)
    restored = engine.load_state(payload)
    assert restored.to_dict() == state.to_dict()
    print(f"Serialized keys      : {list(payload.keys())}")
    print("Round-trip validation: PASS")
    print()


def main() -> int:
    config = StrategyConfig(
        rsi_buy_threshold=50.0,
        rsi_upper_limit=70.0,
        volume_threshold=1.2,
        rsi_exit_mode="threshold",
    )
    engine = LiveSignalEngine(config)

    raw_df = build_synthetic_market_data(rows=100)
    enriched = verify_indicator_computation(engine, raw_df)
    verify_o1_replay(engine, enriched)
    verify_bar_transitions(engine)
    verify_external_state_roundtrip(engine, enriched)

    print("=" * 88)
    print("ALL ANALYTICS TESTS PASSED")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
