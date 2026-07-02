"""Tests for operational kill switches."""

from __future__ import annotations

import pytest

from operational_safety import (
    KillSwitchSettings,
    check_order_placement_allowed,
    collect_open_holdings,
)


def test_trading_paused_blocks_all_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_PAUSED", "true")
    monkeypatch.setenv("ALLOW_NEW_BUYS", "true")
    monkeypatch.setenv("EMERGENCY_LIQUIDATE", "false")
    assert check_order_placement_allowed("BUY") is not None
    assert check_order_placement_allowed("SELL") is not None


def test_allow_new_buys_false_blocks_buy_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_PAUSED", "false")
    monkeypatch.setenv("ALLOW_NEW_BUYS", "false")
    monkeypatch.setenv("EMERGENCY_LIQUIDATE", "false")
    assert check_order_placement_allowed("BUY") is not None
    assert check_order_placement_allowed("SELL") is None


def test_emergency_liquidate_blocks_buy_allows_sell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_PAUSED", "false")
    monkeypatch.setenv("ALLOW_NEW_BUYS", "true")
    monkeypatch.setenv("EMERGENCY_LIQUIDATE", "true")
    monkeypatch.setenv("EMERGENCY_LIQUIDATE_CONFIRM", "I_UNDERSTAND_THIS_WILL_SELL")
    assert check_order_placement_allowed("BUY") is not None
    assert check_order_placement_allowed("SELL") is None


def test_emergency_liquidate_without_confirm_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_PAUSED", "false")
    monkeypatch.setenv("ALLOW_NEW_BUYS", "true")
    monkeypatch.setenv("EMERGENCY_LIQUIDATE", "true")
    monkeypatch.delenv("EMERGENCY_LIQUIDATE_CONFIRM", raising=False)
    from operational_safety import KillSwitchSettings, emergency_liquidate_armed

    assert emergency_liquidate_armed() is False
    assert KillSwitchSettings.from_env().emergency_liquidate is False
    assert check_order_placement_allowed("BUY") is None
    assert check_order_placement_allowed("SELL") is None


def test_emergency_liquidation_sell_bypasses_trading_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_PAUSED", "true")
    monkeypatch.setenv("EMERGENCY_LIQUIDATE", "false")
    assert (
        check_order_placement_allowed(
            "SELL",
            emergency_liquidation_sell=True,
        )
        is None
    )


def test_collect_open_holdings_prefers_broker_snapshot() -> None:
    states = {
        "_portfolio": {"broker_holdings": {"AAPL": 5, "MSFT": 0}},
        "AAPL": {"held_quantity": 3},
        "MSFT": {"held_quantity": 0},
    }
    rows = collect_open_holdings(states, ["AAPL", "MSFT", "NVDA"])
    assert ("AAPL", 5) in rows
    assert all(ticker != "MSFT" for ticker, _qty in rows)


def test_kill_switch_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_PAUSED", raising=False)
    monkeypatch.delenv("ALLOW_NEW_BUYS", raising=False)
    monkeypatch.delenv("EMERGENCY_LIQUIDATE", raising=False)
    settings = KillSwitchSettings.from_env()
    assert settings.trading_paused is False
    assert settings.allow_new_buys is True
    assert settings.emergency_liquidate is False
