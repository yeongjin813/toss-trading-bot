"""Tests for live-account startup guardrails."""

from __future__ import annotations

import pytest

from execution_engine import ExecutionSettings
from kis_environment import load_kis_environment, validate_kis_live_guard
from operational_safety import validate_live_account_requirements


def _live_settings(**overrides: float | int) -> ExecutionSettings:
    base = dict(
        max_daily_loss_usd=5000.0,
        max_open_positions=5,
        max_ticker_exposure_usd=25000.0,
        max_portfolio_usd=100000.0,
        rth_buy_block_open_minutes=10,
        rth_buy_block_close_minutes=5,
        pending_order_stale_minutes=120,
        pending_order_cancel_minutes=45,
        fill_inquiry_alert_cooldown_minutes=15,
        default_limit_buffer_bps=10.0,
        high_vol_limit_buffer_bps=15.0,
        max_consecutive_loss_days=3,
        max_positions_per_sector=2,
    )
    base.update(overrides)
    return ExecutionSettings(**base)


def test_vts_skips_live_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIS_ENVIRONMENT", "vts")
    monkeypatch.delenv("KIS_BASE_URL", raising=False)
    env = load_kis_environment()
    validate_live_account_requirements(
        env,
        dry_run=False,
        capital_at_risk=100_000.0,
        execution_settings=_live_settings(),
    )


def test_live_requires_telegram_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIS_ENVIRONMENT", "live")
    monkeypatch.setenv("KIS_LIVE_CONFIRMED", "true")
    monkeypatch.delenv("KIS_BASE_URL", raising=False)
    monkeypatch.setenv("USE_TELEGRAM_ALERTS", "false")
    env = load_kis_environment()
    validate_kis_live_guard(env, dry_run=False, capital_at_risk=100_000.0)
    with pytest.raises(RuntimeError, match="TELEGRAM"):
        validate_live_account_requirements(
            env,
            dry_run=False,
            capital_at_risk=100_000.0,
            execution_settings=_live_settings(),
        )


def test_live_requires_positive_portfolio_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIS_ENVIRONMENT", "live")
    monkeypatch.setenv("KIS_LIVE_CONFIRMED", "true")
    monkeypatch.delenv("KIS_BASE_URL", raising=False)
    monkeypatch.setenv("USE_TELEGRAM_ALERTS", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    env = load_kis_environment()
    with pytest.raises(RuntimeError, match="MAX_PORTFOLIO_USD"):
        validate_live_account_requirements(
            env,
            dry_run=False,
            capital_at_risk=100_000.0,
            execution_settings=_live_settings(max_portfolio_usd=0.0),
        )


def test_live_passes_with_full_guardrails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIS_ENVIRONMENT", "live")
    monkeypatch.setenv("KIS_LIVE_CONFIRMED", "true")
    monkeypatch.delenv("KIS_BASE_URL", raising=False)
    monkeypatch.setenv("USE_TELEGRAM_ALERTS", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    env = load_kis_environment()
    validate_kis_live_guard(env, dry_run=False, capital_at_risk=100_000.0)
    validate_live_account_requirements(
        env,
        dry_run=False,
        capital_at_risk=100_000.0,
        execution_settings=_live_settings(),
    )
