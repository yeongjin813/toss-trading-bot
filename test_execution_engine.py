"""Tests for fill polling helpers and risk gates."""

from __future__ import annotations

from datetime import datetime

import pytz

from analytics import PositionState
from execution_engine import (
    ExecutionSettings,
    RiskGuard,
    block_new_buy_rth_window,
    summarize_ccnl_fills,
)


def test_summarize_ccnl_fills_partial() -> None:
    rows = [
        {
            "odno": "123",
            "pdno": "AAPL",
            "ft_ccld_qty": "3",
            "ft_ccld_unpr3": "100.00",
        },
        {
            "odno": "123",
            "pdno": "AAPL",
            "ft_ccld_qty": "2",
            "ft_ccld_unpr3": "101.00",
        },
    ]
    qty, avg = summarize_ccnl_fills(rows, odno="123", ticker="AAPL")
    assert qty == 5
    assert round(avg, 2) == 100.40


def test_risk_guard_blocks_max_positions() -> None:
    settings = ExecutionSettings(
        max_daily_loss_usd=100,
        max_open_positions=2,
        max_ticker_exposure_usd=5000,
        rth_buy_block_open_minutes=10,
        rth_buy_block_close_minutes=5,
        pending_order_stale_minutes=120,
        default_limit_buffer_bps=10,
        high_vol_limit_buffer_bps=15,
    )
    guard = RiskGuard(settings)
    states = {
        "AAPL": {"held_quantity": 1, "in_position": True},
        "MSFT": {"held_quantity": 1, "in_position": True},
        "_portfolio": {
            "daily_pnl_anchor_date": "2026-06-16",
            "day_start_equity_usd": 10000,
            "last_equity_usd": 10000,
        },
    }
    reason = guard.check_buy_allowed("NVDA", 1, 200.0, states)
    assert reason is not None
    assert "max open positions" in reason


def test_rth_open_buy_block() -> None:
    settings = ExecutionSettings(
        max_daily_loss_usd=100,
        max_open_positions=3,
        max_ticker_exposure_usd=1000,
        rth_buy_block_open_minutes=10,
        rth_buy_block_close_minutes=5,
        pending_order_stale_minutes=120,
        default_limit_buffer_bps=10,
        high_vol_limit_buffer_bps=15,
    )
    ny = pytz.timezone("America/New_York")
    dt = ny.localize(datetime(2026, 6, 16, 9, 35, 0))
    assert block_new_buy_rth_window(dt, settings) is not None


def test_clear_open_order_fields() -> None:
    runtime = PositionState(
        pending_order=True,
        open_order_id="999",
        open_order_side="BUY",
        open_order_qty=2,
        open_order_price=100.0,
        open_order_submitted_at="2026-06-16T10:00:00",
        open_order_filled_qty=1,
    )
    from execution_engine import clear_open_order

    clear_open_order(runtime)
    assert runtime.pending_order is False
    assert runtime.open_order_id is None
    assert runtime.open_order_qty == 0


def main() -> int:
    test_summarize_ccnl_fills_partial()
    test_risk_guard_blocks_max_positions()
    test_rth_open_buy_block()
    test_clear_open_order_fields()
    print("ALL EXECUTION ENGINE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
