"""End-of-day metrics and Telegram report dispatch for the live bot."""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Any, Mapping

from analytics import _to_ny_datetime, describe_us_market_closure
from telegram_notifier import escape_markdown_v2


def use_daily_telegram_report() -> bool:
    return os.getenv("USE_DAILY_TELEGRAM_REPORT", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_dry_run_mode() -> bool:
    return os.getenv("KIS_DRY_RUN", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def should_send_eod_report(now: datetime | None, states: Mapping[str, Any]) -> bool:
    """True once per NY session date after 16:00 ET on an equity session day."""
    if not use_daily_telegram_report():
        return False

    ny = _to_ny_datetime(now)
    if describe_us_market_closure(ny) is not None:
        return False
    if ny.hour < 16:
        return False

    today = ny.strftime("%Y-%m-%d")
    portfolio = states.get("_portfolio", {})
    if not isinstance(portfolio, dict):
        return False
    return portfolio.get("last_daily_report_date") != today


def _effective_holdings(
    states: Mapping[str, Any],
    watchlist: list[str],
) -> dict[str, int]:
    """Prefer broker snapshot when local fill sync lagged (VTS ccnl 500)."""
    portfolio = states.get("_portfolio", {})
    broker = {}
    if isinstance(portfolio, dict):
        raw = portfolio.get("broker_holdings") or {}
        if isinstance(raw, dict):
            broker = raw

    effective: dict[str, int] = {}
    for ticker in watchlist:
        payload = states.get(ticker, {})
        local_qty = 0
        if isinstance(payload, dict):
            local_qty = int(payload.get("held_quantity", 0) or 0)
        broker_qty = int(broker.get(ticker, 0) or 0)
        effective[ticker] = max(local_qty, broker_qty)
    return effective


def _count_open_positions(states: Mapping[str, Any], watchlist: list[str]) -> int:
    return sum(1 for qty in _effective_holdings(states, watchlist).values() if qty > 0)


def _trades_today(trade_log_path: str, ny_date: str) -> list[dict[str, str]]:
    if not os.path.exists(trade_log_path):
        return []

    rows: list[dict[str, str]] = []
    with open(trade_log_path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            timestamp = (row.get("timestamp") or "").strip()
            if timestamp.startswith(ny_date):
                rows.append(row)
    return rows


def compile_eod_metrics(
    states: Mapping[str, Any],
    watchlist: list[str],
    *,
    trade_log_path: str,
    available_cash: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    ny = _to_ny_datetime(now)
    ny_date = ny.strftime("%Y-%m-%d")
    portfolio = states.get("_portfolio", {})
    if not isinstance(portfolio, dict):
        portfolio = {}

    day_start = float(portfolio.get("day_start_equity_usd", 0.0) or 0.0)
    last_equity = float(portfolio.get("last_equity_usd", day_start) or day_start)
    day_pnl = last_equity - day_start if day_start > 0 else 0.0
    day_pnl_pct = (day_pnl / day_start * 100.0) if day_start > 0 else 0.0

    today_rows = _trades_today(trade_log_path, ny_date)
    fills = [row for row in today_rows if row.get("status") in {"FILLED", "DRY_RUN", "PARTIAL"}]
    buys = sum(1 for row in fills if row.get("signal") == "BUY")
    sells = sum(1 for row in fills if row.get("signal") in {"SELL", "DYNAMIC_ATR_SELL"})

    active = portfolio.get("active_trade_tickers") or []
    if not isinstance(active, list):
        active = []

    holdings: list[str] = []
    for ticker, qty in _effective_holdings(states, watchlist).items():
        if qty > 0:
            holdings.append(f"{ticker}({qty})")

    broker_synced = bool(portfolio.get("last_reconciled_at"))

    return {
        "date": ny_date,
        "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "equity": last_equity,
        "day_start_equity": day_start,
        "fills": len(fills),
        "buys": buys,
        "sells": sells,
        "open_positions": _count_open_positions(states, watchlist),
        "holdings": holdings,
        "active_tickers": [str(t) for t in active],
        "available_cash": available_cash,
        "dry_run": is_dry_run_mode(),
        "last_reconciled_at": portfolio.get("last_reconciled_at"),
        "broker_synced": broker_synced,
    }


def format_eod_report_text(metrics: dict[str, Any]) -> str:
    date_e = escape_markdown_v2(metrics["date"])
    mode = "DRY\\-RUN" if metrics.get("dry_run") else "LIVE"
    pnl = float(metrics["day_pnl"])
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_e = escape_markdown_v2(f"{pnl_sign}{pnl:,.2f}")
    pnl_pct_e = escape_markdown_v2(f"{pnl_sign}{metrics['day_pnl_pct']:.2f}")
    equity_e = escape_markdown_v2(f"{metrics['equity']:,.2f}")
    fills_e = escape_markdown_v2(str(metrics["fills"]))
    buys_e = escape_markdown_v2(str(metrics["buys"]))
    sells_e = escape_markdown_v2(str(metrics["sells"]))
    open_e = escape_markdown_v2(str(metrics["open_positions"]))
    active = metrics.get("active_tickers") or []
    active_e = escape_markdown_v2(", ".join(active) if active else "n/a")
    holdings = metrics.get("holdings") or []
    holdings_e = escape_markdown_v2(", ".join(holdings) if holdings else "none")
    sync_note = ""
    if not metrics.get("broker_synced") and holdings:
        sync_note = "⚠️ Broker sync pending \\(local ledger may lag\\)\n"
    elif not metrics.get("broker_synced"):
        sync_note = "⚠️ Broker sync pending \\(check KIS app vs bot\\)\n"
    cash = metrics.get("available_cash")
    cash_line = ""
    if cash is not None:
        cash_e = escape_markdown_v2(f"{float(cash):,.2f}")
        cash_line = f"Deployable Cash: ${cash_e}\n"

    return (
        f"📊 *EOD Report* \\({date_e}\\) \\[{mode}\\]\n"
        f"Equity: ${equity_e} \\({pnl_pct_e}%\\)\n"
        f"Day PnL: ${pnl_e}\n"
        f"{cash_line}"
        f"Fills: {fills_e} \\(B {buys_e} / S {sells_e}\\)\n"
        f"Open Positions: {open_e}\n"
        f"Held: {holdings_e}\n"
        f"{sync_note}"
        f"Momentum Active: {active_e}"
    )


def mark_eod_report_sent(states: dict[str, Any], *, now: datetime | None = None) -> None:
    ny_date = _to_ny_datetime(now).strftime("%Y-%m-%d")
    portfolio = states.setdefault("_portfolio", {})
    portfolio["last_daily_report_date"] = ny_date
