"""
RTH watchlist orchestration: reconcile → trim → fills → legacy tickers → Top3.

Execution primitives (orders, process_ticker) stay in main.py and are injected
via WatchlistCycleDeps to avoid circular imports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

import requests

from analytics import (
    LiveSignalEngine,
    describe_us_market_closure,
    is_us_equity_session,
    is_us_regular_market_hours,
)
from deployment_config import DeploymentConfig
from execution_engine import OrderFillMonitor, TradeLogWriter, extract_order_odno
from market_data_cache import MarketDataCache
from momentum_ranker import MomentumRankSettings, build_cycle_tickers, rebalance_active_tickers
from overdeployment_trim import format_trim_plan_text, plan_overdeployment_trims
from session_manager import PortfolioLedger
from state_persistence import save_persisted_states
from strategy_ownership import check_buy_collision, claim_ownership, release_ownership
from top3_strategy import compute_top3_rebalance_orders, load_top3_state, save_top3_state

logger = logging.getLogger(__name__)


class KISClient(Protocol):
    def fetch_overseas_present_balance(self, *, natn_cd: str) -> dict[str, Any]: ...


ProcessTickerFn = Callable[..., str]
ReconcileFn = Callable[..., tuple[dict[str, Any], PortfolioLedger]]
RefreshLedgerFn = Callable[..., PortfolioLedger]
MarkPricesFn = Callable[..., dict[str, float]]
EstimateEquityFn = Callable[..., float]
ExecuteOrderFn = Callable[..., dict[str, Any]]
AlertFn = Callable[..., None]
BoolFn = Callable[[], bool]
TelegramFn = Callable[..., None]


@dataclass(frozen=True)
class WatchlistCycleDeps:
    watchlist: list[str]
    momentum_settings: MomentumRankSettings
    deployment: DeploymentConfig
    use_spy_market_filter: bool
    use_qqq_regime_filter: bool
    benchmark_ticker: str
    secondary_benchmark_ticker: str
    capital_at_risk: float
    overdeployment_trim_enabled: bool
    overdeployment_trim_target_pct: float
    fill_monitor: OrderFillMonitor
    trade_log: TradeLogWriter
    process_ticker: ProcessTickerFn
    process_order_retry_queue: Callable[..., None]
    run_session_reconciliation: ReconcileFn
    refresh_ledger_deployable_cash: RefreshLedgerFn
    watchlist_mark_prices: MarkPricesFn
    estimate_portfolio_equity: EstimateEquityFn
    execute_broker_order: ExecuteOrderFn
    dispatch_system_alert: AlertFn
    is_auth_failure: Callable[[BaseException], bool]
    telegram_enabled: BoolFn
    run_telegram: TelegramFn
    send_system_alert: Callable[..., Any]


def run_overdeployment_trim_if_needed(
    client: KISClient,
    cache: MarketDataCache,
    states: dict[str, Any],
    ledger: PortfolioLedger,
    deps: WatchlistCycleDeps,
    *,
    execute: bool = True,
) -> tuple[dict[str, Any], PortfolioLedger]:
    """Trim largest holdings when marked notional exceeds CAPITAL_AT_RISK."""
    if not deps.overdeployment_trim_enabled:
        return states, ledger

    prices = deps.watchlist_mark_prices(cache, deps.watchlist)
    trims, marked, target = plan_overdeployment_trims(
        states,
        deps.watchlist,
        prices,
        capital_at_risk=deps.capital_at_risk,
        target_pct=deps.overdeployment_trim_target_pct,
    )
    if not trims:
        return states, ledger

    plan_text = format_trim_plan_text(
        trims,
        marked=marked,
        target=target,
        capital_at_risk=deps.capital_at_risk,
    )
    print(plan_text)

    portfolio = states.setdefault("_portfolio", {})
    today = datetime.now().strftime("%Y-%m-%d")

    if not execute or not is_us_regular_market_hours():
        print("[TRIM] Deferred until NY regular hours (09:30-16:00 ET)")
        if deps.telegram_enabled():
            deps.run_telegram(
                deps.send_system_alert(
                    "INFO",
                    plan_text.replace("[TRIM]", "Over-deploy trim queued:"),
                )
            )
        return states, ledger

    if portfolio.get("last_overdeployment_trim_date") == today:
        print("[TRIM] Trim already submitted today — skipping")
        return states, ledger

    submitted: list[TrimOrder] = []
    for order in trims:
        try:
            payload = deps.execute_broker_order(
                client,
                order.ticker,
                "SELL",
                order.shares,
                order.reference_price,
            )
            if payload.get("skipped"):
                continue
            odno = extract_order_odno(payload) or ""
            deps.trade_log.append(
                {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ticker": order.ticker,
                    "signal": "SELL",
                    "qty": order.shares,
                    "order_price": order.reference_price,
                    "fill_price": "",
                    "status": "ACCEPTED",
                    "reason": f"overdeploy_trim odno={odno}",
                    "cash_after": ledger.available_cash_usd,
                    "held_qty": int(
                        states.get(order.ticker, {}).get("held_quantity", 0) or 0
                    ),
                }
            )
            submitted.append(order)
        except Exception as exc:
            logger.error(
                "[TRIM/ERROR] %s SELL %s failed: %s",
                order.ticker,
                order.shares,
                exc,
                exc_info=True,
            )

    if not submitted:
        print("[TRIM] No orders accepted by broker — will retry on next RTH cycle")
        return states, ledger

    portfolio["last_overdeployment_trim_date"] = today
    states, ledger = deps.run_session_reconciliation(
        client, states, force=True, cache=cache
    )
    ledger = deps.refresh_ledger_deployable_cash(ledger, states, cache)
    deps.fill_monitor.resolve_all_pending(
        client,
        states,
        deps.watchlist,
        LiveSignalEngine,
        cash_after=ledger.available_cash_usd,
    )
    save_persisted_states(states)

    summary = ", ".join(f"SELL {o.shares} {o.ticker}" for o in submitted)
    print(f"[TRIM] Submitted: {summary}")
    if deps.telegram_enabled():
        deps.run_telegram(
            deps.send_system_alert("INFO", f"Over-deployment trim submitted: {summary}")
        )
    return states, ledger


def run_top3_shadow_cycle(
    client: KISClient,
    cache: MarketDataCache,
    states: dict[str, Any],
    ledger: PortfolioLedger,
    deps: WatchlistCycleDeps,
) -> tuple[dict[str, Any], PortfolioLedger]:
    """Phase 3 shadow or Phase 4 live-split Top3 momentum rebalance."""
    if not deps.deployment.top3_shadow_active:
        return states, ledger

    portfolio_equity = deps.estimate_portfolio_equity(ledger, states, cache)
    shadow = load_top3_state(states)
    broker_holdings = {
        ticker: int(states.get(ticker, {}).get("held_quantity", 0) or 0)
        for ticker in deps.watchlist
    }
    shadow, orders, logs = compute_top3_rebalance_orders(
        cache,
        deps.watchlist,
        shadow,
        total_equity_usd=portfolio_equity,
        deploy=deps.deployment,
        now=datetime.now(),
        settings=deps.momentum_settings,
        broker_holdings=broker_holdings if deps.deployment.top3_live_orders else None,
    )

    if not logs and not orders:
        return states, ledger

    print()
    print("-" * 88)
    print("TOP3 STRATEGY CYCLE".center(88))
    print("-" * 88)
    for line in logs:
        print(line)

    if deps.deployment.top3_live_orders:
        top3_cash = ledger.available_cash_usd * deps.deployment.top3_capital_fraction()
        for order in orders:
            side = "BUY" if order.side == "BUY" else "SELL"
            if side == "BUY":
                est_cost = order.shares * order.reference_price
                if top3_cash <= 0 or est_cost > top3_cash * 1.01:
                    print(
                        f"[TOP3/GATE] {order.ticker} BUY {order.shares} blocked — "
                        f"deployable ${top3_cash:,.2f} < est ${est_cost:,.2f}"
                    )
                    continue
                collision = check_buy_collision(states, order.ticker, "top3")
                if collision:
                    print(
                        f"[TOP3/GATE] {order.ticker} BUY blocked — {collision}"
                    )
                    continue
            try:
                payload = deps.execute_broker_order(
                    client,
                    order.ticker,
                    side,
                    order.shares,
                    order.reference_price,
                )
                odno = extract_order_odno(payload) or ""
                if side == "BUY":
                    claim_ownership(states, order.ticker, "top3")
                elif side == "SELL":
                    held = int(
                        states.get(order.ticker, {}).get("held_quantity", 0) or 0
                    )
                    if held <= order.shares:
                        release_ownership(states, order.ticker)
                deps.trade_log.append(
                    {
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "ticker": order.ticker,
                        "signal": side,
                        "qty": order.shares,
                        "order_price": order.reference_price,
                        "fill_price": "",
                        "status": "ACCEPTED",
                        "reason": f"top3 odno={odno}",
                        "cash_after": ledger.available_cash_usd,
                        "held_qty": 0,
                    }
                )
            except Exception as exc:
                logger.error(
                    "[TOP3/ERROR] %s %s failed: %s",
                    order.ticker,
                    order.side,
                    exc,
                    exc_info=True,
                )
        states, ledger = deps.run_session_reconciliation(
            client, states, force=True, cache=cache
        )
        ledger = deps.refresh_ledger_deployable_cash(ledger, states, cache)
        deps.fill_monitor.resolve_all_pending(
            client,
            states,
            deps.watchlist,
            LiveSignalEngine,
            cash_after=ledger.available_cash_usd,
        )
    elif orders:
        print(
            f"[TOP3/SHADOW] {len(orders)} simulated order(s) — "
            "no KIS dispatch (Phase 3 dry-run)"
        )
        if deps.telegram_enabled():
            summary = ", ".join(
                f"{o.side} {o.shares} {o.ticker}" for o in orders[:5]
            )
            deps.run_telegram(
                deps.send_system_alert(
                    "INFO",
                    f"Top3 shadow rebalance: {summary}",
                )
            )

    save_top3_state(states, shadow)
    print("-" * 88)
    return states, ledger


def run_watchlist_cycle(
    client: KISClient,
    cache: MarketDataCache,
    states: dict[str, Any],
    ledger: PortfolioLedger,
    deps: WatchlistCycleDeps,
) -> tuple[dict[str, Any], PortfolioLedger]:
    """
    One RTH cycle:
      reconcile → trim → fill poll → retry queue → momentum → legacy tickers → Top3
    """
    summary: list[tuple[str, str]] = []

    if not is_us_equity_session():
        closure_reason = describe_us_market_closure() or "market closed"
        print(
            f"[GATE] US calendar session closed ({closure_reason}) — "
            "no live execution; sleeping"
        )
        return states, ledger

    if not is_us_regular_market_hours():
        print(
            "[GATE/RTH] Outside NY regular hours (09:30-16:00 ET) — "
            "skipping API cycle until RTH open"
        )
        return states, ledger

    states, ledger = deps.run_session_reconciliation(client, states, cache=cache)
    ledger = deps.refresh_ledger_deployable_cash(ledger, states, cache)
    states, ledger = run_overdeployment_trim_if_needed(
        client, cache, states, ledger, deps, execute=True
    )
    deps.fill_monitor.resolve_all_pending(
        client,
        states,
        deps.watchlist,
        LiveSignalEngine,
        cash_after=ledger.available_cash_usd,
    )
    save_persisted_states(states)
    deps.process_order_retry_queue(client, states, ledger)

    momentum_snapshot = rebalance_active_tickers(
        cache,
        deps.watchlist,
        states,
        settings=deps.momentum_settings,
    )
    active_trade_tickers = frozenset(momentum_snapshot.active_tickers)
    cycle_tickers = build_cycle_tickers(
        deps.watchlist,
        momentum_snapshot.active_tickers,
        states,
    )
    if deps.momentum_settings.enabled:
        skipped = [t for t in deps.watchlist if t not in cycle_tickers]
        if skipped:
            print(
                f"[MOMENTUM] Skipping flat tickers outside Top "
                f"{deps.momentum_settings.top_n}: {', '.join(skipped)}"
            )

    refresh_tickers = list(cycle_tickers)
    if deps.use_spy_market_filter:
        refresh_tickers.append(deps.benchmark_ticker)
    if deps.use_qqq_regime_filter:
        refresh_tickers.append(deps.secondary_benchmark_ticker)
    cycle_timestamp = datetime.now()
    refresh_map = cache.refresh_latest_parallel(refresh_tickers, now=cycle_timestamp)
    print(
        f"[CYCLE] Refreshed {len(refresh_map)} tickers in parallel "
        f"at {cycle_timestamp.strftime('%H:%M:%S')}"
    )

    for ticker in cycle_tickers:
        frame, is_new = refresh_map.get(
            ticker,
            (cache.evaluation_frame(ticker), False),
        )
        try:
            signal = deps.process_ticker(
                client,
                cache,
                ticker,
                states,
                ledger,
                active_trade_tickers=active_trade_tickers,
                market_frame=frame,
                is_new_bar=is_new,
                cycle_timestamp=cycle_timestamp,
            )
            summary.append((ticker, signal))
        except requests.Timeout as exc:
            print(f"[ERROR] Timeout for {ticker}: {exc}")
            deps.dispatch_system_alert("WARNING", f"{ticker} KIS request timeout: {exc}")
            summary.append((ticker, "TIMEOUT"))
        except requests.RequestException as exc:
            print(f"[ERROR] Network failure for {ticker}: {exc}")
            alert_level = "CRITICAL" if deps.is_auth_failure(exc) else "WARNING"
            deps.dispatch_system_alert(
                alert_level,
                f"{ticker} KIS API connection failure: {exc}",
            )
            summary.append((ticker, "NETWORK_ERROR"))
        except Exception as exc:
            logger.error(
                "[TICKER/ERROR] Unhandled failure for %s: %s",
                ticker,
                exc,
                exc_info=True,
            )
            deps.dispatch_system_alert(
                "CRITICAL",
                f"{ticker} unhandled trading loop failure: {exc}",
            )
            summary.append((ticker, "FAILED"))

    states, ledger = run_top3_shadow_cycle(client, cache, states, ledger, deps)
    cache.maybe_collect_garbage()
    save_persisted_states(states)

    print()
    print("=" * 88)
    print("WATCHLIST CYCLE SUMMARY".center(88))
    print("=" * 88)
    for ticker, status in summary:
        print(f"  {ticker:<6} -> {status}")
    print("=" * 88)

    return states, ledger
