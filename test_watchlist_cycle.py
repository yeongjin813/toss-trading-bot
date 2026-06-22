"""Tests for watchlist cycle orchestration gates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import MagicMock

from deployment_config import DeploymentConfig
from momentum_ranker import MomentumRankSettings
from session_manager import PortfolioLedger
from watchlist_cycle import WatchlistCycleDeps, run_overdeployment_trim_if_needed, run_watchlist_cycle


@dataclass
class _DepsRecorder:
    reconcile_called: bool = False
    process_ticker_called: bool = False


def _minimal_deps(recorder: _DepsRecorder) -> WatchlistCycleDeps:
    deployment = DeploymentConfig(
        phase=4,
        strategy_mode="dual",
        top3_backtest_only=False,
        top3_dry_run_enabled=False,
        legacy_capital_pct=60.0,
        top3_capital_pct=40.0,
    )
    momentum = MomentumRankSettings(enabled=False, top_n=3)

    def _reconcile(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], PortfolioLedger]:
        recorder.reconcile_called = True
        return {}, PortfolioLedger()

    def _process_ticker(*_args: Any, **_kwargs: Any) -> str:
        recorder.process_ticker_called = True
        return "HOLD"

    return WatchlistCycleDeps(
        watchlist=["AAPL"],
        momentum_settings=momentum,
        deployment=deployment,
        use_spy_market_filter=False,
        use_qqq_regime_filter=False,
        benchmark_ticker="SPY",
        secondary_benchmark_ticker="QQQ",
        capital_at_risk=100_000.0,
        overdeployment_trim_enabled=False,
        overdeployment_trim_target_pct=0.98,
        fill_monitor=MagicMock(),
        trade_log=MagicMock(),
        process_ticker=_process_ticker,
        process_order_retry_queue=lambda *_a, **_k: None,
        run_session_reconciliation=_reconcile,
        refresh_ledger_deployable_cash=lambda ledger, *_a, **_k: ledger,
        watchlist_mark_prices=lambda *_a, **_k: {},
        estimate_portfolio_equity=lambda *_a, **_k: 100_000.0,
        execute_broker_order=MagicMock(),
        dispatch_system_alert=lambda *_a, **_k: None,
        is_auth_failure=lambda _e: False,
        telegram_enabled=lambda: False,
        run_telegram=lambda *_a, **_k: None,
        send_system_alert=MagicMock(),
    )


def test_skips_reconcile_outside_rth(monkeypatch) -> None:
    recorder = _DepsRecorder()
    deps = _minimal_deps(recorder)
    monkeypatch.setattr("watchlist_cycle.is_us_equity_session", lambda: True)
    monkeypatch.setattr("watchlist_cycle.is_us_regular_market_hours", lambda: False)

    states, ledger = run_watchlist_cycle(
        MagicMock(),
        MagicMock(),
        {},
        PortfolioLedger(),
        deps,
    )

    assert recorder.reconcile_called is False
    assert recorder.process_ticker_called is False
    assert ledger.available_cash_usd == 0.0


def test_skips_reconcile_on_calendar_holiday(monkeypatch) -> None:
    recorder = _DepsRecorder()
    deps = _minimal_deps(recorder)
    monkeypatch.setattr("watchlist_cycle.is_us_equity_session", lambda: False)

    run_watchlist_cycle(MagicMock(), MagicMock(), {}, PortfolioLedger(), deps)

    assert recorder.reconcile_called is False
    assert recorder.process_ticker_called is False


def test_trim_retries_when_broker_rejects_all_orders(monkeypatch) -> None:
    states: dict[str, Any] = {
        "TSM": {"held_quantity": 73, "in_position": True},
        "TSLA": {"held_quantity": 69, "in_position": True},
        "AMD": {"held_quantity": 49, "in_position": True},
        "CRWD": {"held_quantity": 38, "in_position": True},
        "_portfolio": {"last_overdeployment_trim_date": "2099-01-01"},
    }
    recorder = _DepsRecorder()
    order_mock = MagicMock(side_effect=RuntimeError("rejected"))
    deps = replace(
        _minimal_deps(recorder),
        overdeployment_trim_enabled=True,
        watchlist=["TSM", "TSLA", "AMD", "CRWD"],
        watchlist_mark_prices=lambda *_a, **_k: {
            "TSM": 460.0,
            "TSLA": 400.0,
            "AMD": 535.0,
            "CRWD": 685.0,
        },
        execute_broker_order=order_mock,
        run_session_reconciliation=lambda s, *_a, **_k: (s, PortfolioLedger()),
        refresh_ledger_deployable_cash=lambda ledger, *_a, **_k: ledger,
    )
    monkeypatch.setattr("watchlist_cycle.is_us_regular_market_hours", lambda: True)
    monkeypatch.setattr("watchlist_cycle.save_persisted_states", lambda *_a, **_k: None)

    states, _ = run_overdeployment_trim_if_needed(
        MagicMock(),
        MagicMock(),
        states,
        PortfolioLedger(),
        deps,
        execute=True,
    )

    assert states["_portfolio"].get("last_overdeployment_trim_date") == "2099-01-01"
    order_mock.assert_called()
