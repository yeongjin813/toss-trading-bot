"""
P3 live execution hardening: RTH gates, intraday session-low ATR tracking,
and broker-vs-local portfolio reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from analytics import (
    BarSnapshot,
    LiveSignalEngine,
    PositionState,
    is_us_regular_market_hours,
    use_eod_atr_stops,
)
from config import StrategyConfigMapper


@dataclass
class ReconciliationMismatch:
    ticker: str
    local_quantity: int
    broker_quantity: int
    delta: int


@dataclass
class ReconciliationReport:
    timestamp: str
    broker_cash_usd: float
    available_cash_usd: float
    broker_holdings: dict[str, int]
    mismatches: list[ReconciliationMismatch] = field(default_factory=list)
    reconciled: bool = True
    error: str | None = None

    @property
    def has_mismatch(self) -> bool:
        return len(self.mismatches) > 0


@dataclass
class PortfolioLedger:
    """Consolidated broker-aligned cash ledger for live sizing."""

    broker_cash_usd: float = 0.0
    available_cash_usd: float = 0.0
    last_reconciled_at: str | None = None
    last_report: ReconciliationReport | None = None


class RegularHoursGate:
    """
    Regular Trading Hours (RTH) enforcement - 09:30-16:00 America/New_York.

    Matches daily backtest assumptions: no entries, crossover exits, or ATR
    stop dispatches outside RTH.
    """

    @staticmethod
    def is_rth(now: datetime | None = None) -> bool:
        return is_us_regular_market_hours(now)

    @staticmethod
    def block_signal_for_session(signal: str, regular_market_hours: bool) -> bool:
        """True when the signal must not be dispatched due to session boundary."""
        if regular_market_hours:
            return False
        return signal in {"BUY", "SELL", "DYNAMIC_ATR_SELL"}

    @staticmethod
    def gate_message(signal: str, ticker: str) -> str:
        return (
            f"[GATE/RTH] {ticker} - {signal} blocked outside NY regular session "
            f"(09:30-16:00 ET). Matching daily backtest session boundary."
        )


class IntradaySessionTracker:
    """
    Real-time session low tracker for intraday ATR stop parity.

    Daily backtest triggers on bar Low; live engine tracks running minimum of
    bar low and current mark price during RTH only.
    """

    @staticmethod
    def update_session_low(
        runtime: PositionState,
        bar: BarSnapshot,
        *,
        current_price: float | None = None,
        regular_market_hours: bool = True,
    ) -> float:
        """
        Update running session low for the active daily bar.

        During RTH: session_low = min(prior, bar.low, current_price).
        Outside RTH: session_low is frozen (no pre/post-market stop triggers).

        When ``USE_EOD_ATR_STOPS=true``, returns the static daily bar low only.
        """
        if use_eod_atr_stops():
            return float(bar.low)

        bar_date_str = (
            bar.date.strftime("%Y-%m-%d")
            if hasattr(bar.date, "strftime")
            else str(bar.date)
        )

        if not regular_market_hours:
            if runtime.session_low is None:
                runtime.session_low = bar.low
            return float(runtime.session_low)

        tick_candidates = [float(bar.low)]
        if current_price is not None and current_price > 0:
            tick_candidates.append(float(current_price))

        session_tick_low = min(tick_candidates)

        if runtime.latest_bar_date != bar_date_str:
            runtime.latest_bar_date = bar_date_str
            runtime.session_low = session_tick_low
        elif runtime.session_low is None:
            runtime.session_low = session_tick_low
        else:
            runtime.session_low = min(float(runtime.session_low), session_tick_low)

        return float(runtime.session_low)

    @staticmethod
    def evaluate_intraday_atr_stop(
        engine: LiveSignalEngine,
        runtime: PositionState,
        bar: BarSnapshot,
        session_low: float,
        *,
        regular_market_hours: bool,
    ) -> dict[str, Any] | None:
        """
        Priority-1 intraday ATR scan during RTH.

        Returns DYNAMIC_ATR_SELL payload when session_low <= trigger_floor.
        """
        if use_eod_atr_stops():
            return None
        if not regular_market_hours:
            return None
        if not (runtime.in_position or runtime.held_quantity > 0):
            return None
        if runtime.pending_order:
            return None
        if runtime.trigger_floor is None:
            return None

        return engine.evaluate_intraday_atr_stop(
            runtime,
            bar,
            session_low=session_low,
            mutate_state=False,
        )


def _parse_broker_holdings(
    payload: dict[str, Any],
    watchlist: list[str],
) -> dict[str, int]:
    """
    Extract per-ticker quantities from KIS inquire-present-balance output1.

    Handles common VTS field aliases for overseas equity holdings.
    """
    holdings: dict[str, int] = {ticker: 0 for ticker in watchlist}
    rows = payload.get("output1") or []

    if not isinstance(rows, list):
        return holdings

    watchset = set(watchlist)
    qty_fields = (
        "ovrs_stck_tot_qty",
        "hldg_qty",
        "cblc_qty",
        "ovrs_cblc_qty",
        "tot_qty",
        "ord_psbl_qty",
    )
    symbol_fields = ("ovrs_pdno", "pdno", "symb", "prdt_name")

    for row in rows:
        if not isinstance(row, dict):
            continue

        symbol = None
        for field_name in symbol_fields:
            raw = row.get(field_name)
            if raw:
                symbol = str(raw).strip().upper()
                break
        if not symbol or symbol not in watchset:
            continue

        quantity = 0
        for field_name in qty_fields:
            raw = row.get(field_name)
            if raw in (None, ""):
                continue
            try:
                quantity = int(float(str(raw).replace(",", "")))
                break
            except (TypeError, ValueError):
                continue

        holdings[symbol] = max(quantity, 0)

    return holdings


def _parse_broker_cash(payload: dict[str, Any]) -> tuple[float, float]:
    """Return (broker_cash_usd, available_cash_usd) from present-balance payload."""
    broker_cash = 0.0
    available_cash = 0.0

    output2_rows = payload.get("output2") or []
    if isinstance(output2_rows, list) and output2_rows:
        usd_row = next(
            (row for row in output2_rows if isinstance(row, dict) and row.get("crcy_cd") == "USD"),
            output2_rows[0] if isinstance(output2_rows[0], dict) else {},
        )
        if isinstance(usd_row, dict):
            broker_cash = float(
                usd_row.get("frcr_dncl_amt_2")
                or usd_row.get("frcr_drwg_psbl_amt_1")
                or 0
            )
            available_cash = float(
                usd_row.get("frcr_drwg_psbl_amt_1")
                or usd_row.get("frcr_use_psbl_amt")
                or broker_cash
            )

    output3 = payload.get("output3") or {}
    if isinstance(output3, list):
        output3 = output3[0] if output3 else {}
    if isinstance(output3, dict):
        foreign_use = output3.get("frcr_use_psbl_amt")
        if foreign_use not in (None, ""):
            try:
                parsed = float(foreign_use)
                if parsed > 0:
                    available_cash = max(available_cash, parsed)
            except (TypeError, ValueError):
                pass

    return broker_cash, available_cash


class PortfolioReconciliationEngine:
    """
    Synchronize local trading_state.json with broker-held quantities and cash.

    Runs at session start and before each RTH watchlist cycle to prevent drift
    from partial fills, manual trades, or API/state desync.
    """

    def __init__(self, watchlist: list[str]) -> None:
        self.watchlist = watchlist

    def reconcile(
        self,
        client: Any,
        states: dict[str, Any],
        *,
        fallback_cash: float,
    ) -> tuple[dict[str, Any], PortfolioLedger, ReconciliationReport]:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            payload = client.fetch_overseas_present_balance(natn_cd="840")
            broker_holdings = _parse_broker_holdings(payload, self.watchlist)
            broker_cash, available_cash = _parse_broker_cash(payload)

            if available_cash <= 0 and broker_cash > 0:
                available_cash = broker_cash
            if available_cash <= 0:
                available_cash = fallback_cash

            mismatches: list[ReconciliationMismatch] = []

            for ticker in self.watchlist:
                broker_qty = int(broker_holdings.get(ticker, 0))
                ticker_state = states.setdefault(ticker, {})
                local_qty = int(ticker_state.get("held_quantity", 0) or 0)

                if broker_qty != local_qty:
                    delta = broker_qty - local_qty
                    mismatches.append(
                        ReconciliationMismatch(
                            ticker=ticker,
                            local_quantity=local_qty,
                            broker_quantity=broker_qty,
                            delta=delta,
                        )
                    )
                    print(
                        f"[RECONCILE/MISMATCH] {ticker} local={local_qty} "
                        f"broker={broker_qty} delta={delta:+d} - overriding local ledger"
                    )
                    ticker_state["held_quantity"] = broker_qty
                    ticker_state["in_position"] = broker_qty > 0
                    if broker_qty <= 0 and not ticker_state.get("pending_order"):
                        ticker_state["highest_price_achieved"] = None
                        ticker_state["current_atr"] = None
                        ticker_state["dynamic_stop_distance"] = None
                        ticker_state["trigger_floor"] = None
                        ticker_state["session_low"] = None
                    elif broker_qty > 0 and not ticker_state.get("in_position"):
                        ticker_state["in_position"] = True

            if mismatches:
                print(
                    f"[RECONCILE] {len(mismatches)} mismatch(es) corrected - "
                    "local state aligned to broker registry"
                )
            else:
                print("[RECONCILE] Broker holdings match local ledger - no override required")

            print(
                f"[RECONCILE] Broker cash USD={broker_cash:,.2f} | "
                f"Available for deployment={available_cash:,.2f}"
            )

            report = ReconciliationReport(
                timestamp=timestamp,
                broker_cash_usd=broker_cash,
                available_cash_usd=available_cash,
                broker_holdings=broker_holdings,
                mismatches=mismatches,
                reconciled=True,
            )
            ledger = PortfolioLedger(
                broker_cash_usd=broker_cash,
                available_cash_usd=available_cash,
                last_reconciled_at=timestamp,
                last_report=report,
            )
            states["_portfolio"] = {
                "broker_cash_usd": broker_cash,
                "available_cash_usd": available_cash,
                "last_reconciled_at": timestamp,
                "broker_holdings": broker_holdings,
            }
            return states, ledger, report

        except Exception as exc:
            print(f"[RECONCILE/ERROR] Broker sync failed: {exc}")
            cached = states.get("_portfolio", {})
            available = float(cached.get("available_cash_usd", fallback_cash) or fallback_cash)
            report = ReconciliationReport(
                timestamp=timestamp,
                broker_cash_usd=float(cached.get("broker_cash_usd", 0.0) or 0.0),
                available_cash_usd=available,
                broker_holdings={
                    ticker: int(states.get(ticker, {}).get("held_quantity", 0) or 0)
                    for ticker in self.watchlist
                },
                mismatches=[],
                reconciled=False,
                error=str(exc),
            )
            ledger = PortfolioLedger(
                broker_cash_usd=report.broker_cash_usd,
                available_cash_usd=available,
                last_reconciled_at=cached.get("last_reconciled_at"),
                last_report=report,
            )
            return states, ledger, report


class LiveExecutionGatekeeper:
    """
    Orchestrates RTH enforcement, intraday ATR scanning, and signal routing
    for a single ticker processing cycle.
    """

    def __init__(self) -> None:
        self.rth_gate = RegularHoursGate()
        self.session_tracker = IntradaySessionTracker()

    def evaluate_live_signals(
        self,
        engine: LiveSignalEngine,
        runtime: PositionState,
        cycle: dict[str, Any],
        *,
        current_price: float | None = None,
    ) -> dict[str, Any]:
        """
        Apply P3 session rules on top of evaluate_trading_cycle output.

        - Intraday ATR stop checked first during RTH when positioned
        - All dispatchable signals blocked outside RTH
        """
        regular_market_hours = bool(
            cycle.get("regular_market_hours", cycle.get("market_open", False))
        )
        today_metrics = cycle["metrics"]["today"]
        yesterday_metrics = cycle["metrics"]["yesterday"]

        today = BarSnapshot(
            date=today_metrics["date"],
            open=float(today_metrics.get("open", today_metrics["close"])),
            high=float(today_metrics.get("high", today_metrics["close"])),
            low=float(today_metrics.get("low", today_metrics["close"])),
            close=float(today_metrics.get("close")),
            volume=float(today_metrics["volume"]),
            sma_short=float(today_metrics["sma_short"]),
            sma_long=float(today_metrics["sma_long"]),
            rsi=float(today_metrics["rsi"]),
            atr=float(today_metrics["atr"]),
            volume_sma=float(today_metrics["volume_sma"]),
        )
        yesterday = BarSnapshot(
            date=yesterday_metrics["date"],
            open=float(yesterday_metrics.get("open", yesterday_metrics["close"])),
            high=float(yesterday_metrics.get("high", yesterday_metrics["close"])),
            low=float(yesterday_metrics.get("low", yesterday_metrics["close"])),
            close=float(yesterday_metrics["close"]),
            volume=float(yesterday_metrics["volume"]),
            sma_short=float(yesterday_metrics["sma_short"]),
            sma_long=float(yesterday_metrics["sma_long"]),
            rsi=float(yesterday_metrics["rsi"]),
            atr=float(yesterday_metrics["atr"]),
            volume_sma=float(yesterday_metrics["volume_sma"]),
        )

        mark_price = current_price if current_price is not None else today.close
        session_low = self.session_tracker.update_session_low(
            runtime,
            today,
            current_price=mark_price,
            regular_market_hours=regular_market_hours,
        )
        cycle["session_low"] = session_low
        cycle["runtime_state"] = runtime.to_dict()

        signal_result = dict(cycle["signal_result"])
        base_signal = signal_result.get("signal", "HOLD")

        if runtime.pending_order:
            return signal_result

        intraday_stop = self.session_tracker.evaluate_intraday_atr_stop(
            engine,
            runtime,
            today,
            session_low,
            regular_market_hours=regular_market_hours,
        )
        if intraday_stop is not None:
            signal_result = intraday_stop
            signal_result["intraday_atr_scan"] = True
            cycle["signal_result"] = signal_result
            return signal_result

        if self.rth_gate.block_signal_for_session(base_signal, regular_market_hours):
            signal_result = {
                "signal": "HOLD",
                "liquidity_ok": signal_result.get("liquidity_ok", False),
                "crossover_suppressed": True,
                "outside_regular_hours": True,
                "blocked_signal": base_signal,
            }
            cycle["signal_result"] = signal_result
            return signal_result

        if (
            regular_market_hours
            and (runtime.in_position or runtime.held_quantity > 0)
            and base_signal == "HOLD"
        ):
            allow_crossover = bool(cycle.get("allow_crossover", False))
            crossover_eval = engine.evaluate_bar(
                runtime,
                today,
                yesterday,
                mutate_state=False,
                allow_crossover=allow_crossover,
                session_low=session_low,
            )
            crossover_signal = crossover_eval.get("signal", "HOLD")
            if crossover_signal != "HOLD":
                signal_result = crossover_eval
                cycle["signal_result"] = signal_result
                return signal_result

        cycle["signal_result"] = signal_result
        return signal_result
