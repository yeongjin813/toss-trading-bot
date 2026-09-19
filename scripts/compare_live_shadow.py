#!/usr/bin/env python3
"""
Compare live equity_history vs Top3 shadow (read-only).

Usage:
  python scripts/compare_live_shadow.py
  python scripts/compare_live_shadow.py --state ./trading_state.json --csv ./tmp/live_shadow.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"state file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("trading_state.json root must be an object")
    return raw


def compare(state: dict) -> dict:
    portfolio = state.get("_portfolio") or {}
    history = portfolio.get("equity_history") or {}
    if not isinstance(history, dict):
        history = {}
    dates = sorted(str(k) for k in history.keys())
    live_first = float(history[dates[0]]) if dates else None
    live_last = float(history[dates[-1]]) if dates else None
    live_n = len(dates)

    shadow = portfolio.get("_top3_shadow") or {}
    shadow_eq = shadow.get("last_equity_usd")
    shadow_rebal = shadow.get("last_rebalance_date")
    shadow_tickers = shadow.get("active_tickers") or []

    last_equity = portfolio.get("last_equity_usd")
    paper_anchor = portfolio.get("paper_anchor_equity_usd")

    live_chg = None
    if live_first is not None and live_last is not None and live_first != 0:
        live_chg = (live_last / live_first - 1.0) * 100.0

    return {
        "live_days": live_n,
        "live_first_date": dates[0] if dates else None,
        "live_last_date": dates[-1] if dates else None,
        "live_first_equity": live_first,
        "live_last_equity": live_last,
        "live_change_pct": live_chg,
        "last_equity_usd": last_equity,
        "paper_anchor_equity_usd": paper_anchor,
        "shadow_last_equity_usd": shadow_eq,
        "shadow_last_rebalance_date": shadow_rebal,
        "shadow_active_tickers": ",".join(str(t) for t in shadow_tickers),
        "gap_live_vs_shadow": (
            float(live_last) - float(shadow_eq)
            if live_last is not None and shadow_eq is not None
            else None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        type=Path,
        default=ROOT / "trading_state.json",
        help="Path to trading_state.json",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Optional CSV output path",
    )
    args = parser.parse_args()
    summary = compare(_load_state(args.state))

    print("=== Live vs Top3 shadow ===")
    for key, value in summary.items():
        print(f"{key}: {value}")

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(summary.keys()))
            writer.writeheader()
            writer.writerow(summary)
        print(f"wrote {args.csv}")


if __name__ == "__main__":
    main()
