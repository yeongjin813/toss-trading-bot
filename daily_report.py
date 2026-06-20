"""End-of-day metrics and Telegram report dispatch for the live bot."""

from __future__ import annotations

import csv
import os
from datetime import datetime, timedelta
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


def use_weekly_telegram_report() -> bool:
    return os.getenv("USE_WEEKLY_TELEGRAM_REPORT", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def record_daily_equity_snapshot(
    states: dict[str, Any],
    equity: float,
    *,
    now: datetime | None = None,
) -> None:
    """Append one EOD equity point for weekly paper-trading reports."""
    ny_date = _to_ny_datetime(now).strftime("%Y-%m-%d")
    portfolio = states.setdefault("_portfolio", {})
    history = portfolio.get("equity_history")
    if not isinstance(history, dict):
        history = {}
    history[ny_date] = round(float(equity), 2)
    if len(history) > 120:
        for key in sorted(history.keys())[:-120]:
            history.pop(key, None)
    portfolio["equity_history"] = history


def _trades_since(trade_log_path: str, since_date: str) -> list[dict[str, str]]:
    if not os.path.exists(trade_log_path):
        return []
    rows: list[dict[str, str]] = []
    with open(trade_log_path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            timestamp = (row.get("timestamp") or "").strip()
            if timestamp[:10] >= since_date:
                rows.append(row)
    return rows


def should_send_weekly_report(now: datetime | None, states: Mapping[str, Any]) -> bool:
    """Friday after 16:00 ET, once per ISO week — paper-trading go/no-go summary."""
    if not use_weekly_telegram_report():
        return False
    ny = _to_ny_datetime(now)
    if describe_us_market_closure(ny) is not None:
        return False
    if ny.weekday() != 4 or ny.hour < 16:
        return False
    portfolio = states.get("_portfolio", {})
    if not isinstance(portfolio, dict):
        return False
    week_key = ny.strftime("%G-W%V")
    return portfolio.get("last_weekly_report_week") != week_key


def compile_weekly_metrics(
    states: Mapping[str, Any],
    watchlist: list[str],
    *,
    trade_log_path: str,
    available_cash: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    ny = _to_ny_datetime(now)
    ny_date = ny.strftime("%Y-%m-%d")
    week_key = ny.strftime("%G-W%V")
    portfolio = states.get("_portfolio", {})
    if not isinstance(portfolio, dict):
        portfolio = {}

    history = portfolio.get("equity_history") or {}
    if not isinstance(history, dict):
        history = {}

    since = (ny - timedelta(days=7)).strftime("%Y-%m-%d")
    week_dates = sorted(
        d
        for d in history.keys()
        if d <= ny_date and d >= (ny - timedelta(days=7)).strftime("%Y-%m-%d")
    )
    week_start_equity = float(history[week_dates[0]]) if week_dates else float(
        portfolio.get("last_equity_usd", 0.0) or 0.0
    )
    week_end_equity = float(history[week_dates[-1]]) if week_dates else week_start_equity
    week_pnl = week_end_equity - week_start_equity
    week_pnl_pct = (week_pnl / week_start_equity * 100.0) if week_start_equity > 0 else 0.0

    week_rows = _trades_since(trade_log_path, since)
    fills = [r for r in week_rows if r.get("status") in {"FILLED", "DRY_RUN", "PARTIAL"}]
    buys = sum(1 for r in fills if r.get("signal") == "BUY")
    sells = sum(1 for r in fills if r.get("signal") in {"SELL", "DYNAMIC_ATR_SELL"})

    anchor = float(portfolio.get("paper_anchor_equity_usd", 0.0) or 0.0)
    if anchor <= 0 and week_start_equity > 0:
        anchor = week_start_equity
    cumulative_pnl = week_end_equity - anchor if anchor > 0 else 0.0
    cumulative_pct = (cumulative_pnl / anchor * 100.0) if anchor > 0 else 0.0

    return {
        "week_key": week_key,
        "week_end_date": ny_date,
        "week_start_equity": week_start_equity,
        "week_end_equity": week_end_equity,
        "week_pnl": week_pnl,
        "week_pnl_pct": week_pnl_pct,
        "cumulative_pnl": cumulative_pnl,
        "cumulative_pnl_pct": cumulative_pct,
        "paper_anchor_equity": anchor,
        "fills": len(fills),
        "buys": buys,
        "sells": sells,
        "open_positions": _count_open_positions(states, watchlist),
        "equity_days": len(week_dates),
        "available_cash": available_cash,
        "dry_run": is_dry_run_mode(),
    }


def format_weekly_report_text(metrics: dict[str, Any]) -> str:
    week_e = escape_markdown_v2(metrics["week_key"])
    end_e = escape_markdown_v2(metrics["week_end_date"])
    mode = "DRY\\-RUN" if metrics.get("dry_run") else "PAPER"
    pnl = float(metrics["week_pnl"])
    pnl_sign = "+" if pnl >= 0 else ""
    week_pnl_e = escape_markdown_v2(f"{pnl_sign}{pnl:,.2f}")
    week_pct_e = escape_markdown_v2(f"{pnl_sign}{metrics['week_pnl_pct']:.2f}")
    cum_pnl = float(metrics["cumulative_pnl"])
    cum_sign = "+" if cum_pnl >= 0 else ""
    cum_pnl_e = escape_markdown_v2(f"{cum_sign}{cum_pnl:,.2f}")
    cum_pct_e = escape_markdown_v2(f"{cum_sign}{metrics['cumulative_pnl_pct']:.2f}")
    equity_e = escape_markdown_v2(f"{metrics['week_end_equity']:,.2f}")
    fills_e = escape_markdown_v2(str(metrics["fills"]))
    open_e = escape_markdown_v2(str(metrics["open_positions"]))
    return (
        f"📈 *Weekly Paper Report* \\({week_e}, {end_e}\\) \\[{mode}\\]\n"
        f"Equity: ${equity_e}\n"
        f"Week PnL: ${week_pnl_e} \\({week_pct_e}%\\)\n"
        f"Since anchor: ${cum_pnl_e} \\({cum_pct_e}%\\)\n"
        f"Fills: {fills_e} \\(B {escape_markdown_v2(str(metrics['buys']))} / "
        f"S {escape_markdown_v2(str(metrics['sells']))}\\)\n"
        f"Open Positions: {open_e}\n"
        f"Equity snapshots: {escape_markdown_v2(str(metrics['equity_days']))}/7d"
    )


def mark_weekly_report_sent(states: dict[str, Any], *, now: datetime | None = None) -> None:
    ny = _to_ny_datetime(now)
    portfolio = states.setdefault("_portfolio", {})
    portfolio["last_weekly_report_week"] = ny.strftime("%G-W%V")
    anchor = float(portfolio.get("paper_anchor_equity_usd", 0.0) or 0.0)
    if anchor <= 0:
        history = portfolio.get("equity_history") or {}
        if isinstance(history, dict) and history:
            portfolio["paper_anchor_equity_usd"] = float(sorted(history.items())[0][1])
        elif portfolio.get("last_equity_usd"):
            portfolio["paper_anchor_equity_usd"] = float(portfolio["last_equity_usd"])
