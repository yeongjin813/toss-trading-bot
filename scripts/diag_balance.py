"""One-off KIS present-balance vs ccnl diagnostic (run on EC2)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from main import KISApiClient as KISClient
from market_registry import parse_watchlist
from session_manager import (
    _holdings_from_ccnl,
    _holdings_from_inquire_balance,
    _parse_broker_holdings,
    resolve_broker_holdings,
)

wl = parse_watchlist(os.getenv("WATCHLIST"))
client = KISClient()
payload = client.fetch_overseas_present_balance(natn_cd="840")

print("=== output1 rows with pdno ===")
for row in payload.get("output1") or []:
    if not isinstance(row, dict):
        continue
    sym = row.get("pdno") or row.get("ovrs_pdno")
    if sym:
        print(json.dumps(row, ensure_ascii=False))

print("\n=== parsed present-balance (nonzero) ===")
parsed = _parse_broker_holdings(payload, wl)
print({k: v for k, v in parsed.items() if v > 0})

print("\n=== inquire-balance (nonzero) ===")
inq = _holdings_from_inquire_balance(client, wl)
print({k: v for k, v in inq.items() if v > 0})

print("\n=== ccnl aggregation (nonzero) ===")
ccnl = _holdings_from_ccnl(client, wl)
print({k: v for k, v in ccnl.items() if v > 0})

print("\n=== resolve_broker_holdings (nonzero) ===")
resolved = resolve_broker_holdings(client, wl, payload)
print({k: v for k, v in resolved.items() if v > 0})
