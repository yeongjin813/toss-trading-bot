"""Dump recent TSM ccnl rows from KIS VTS."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from main import KISApiClient

client = KISApiClient()
end = datetime.now()
start = end - timedelta(days=30)
rows = client.fetch_overseas_order_ccnl(
    start.strftime("%Y%m%d"),
    end.strftime("%Y%m%d"),
)
tsm = [r for r in rows if str(r.get("pdno", "")).upper() == "TSM"]
print(f"TSM ccnl rows: {len(tsm)}")
for row in tsm[-15:]:
    print(json.dumps({
        "ord_dt": row.get("ord_dt"),
        "odno": row.get("odno"),
        "sll_buy": row.get("sll_buy_dvsn_cd") or row.get("sll_buy_dvsn"),
        "ccld_qty": row.get("ccld_qty"),
        "ord_qty": row.get("ord_qty"),
        "ccld_nccs_dvsn": row.get("ccld_nccs_dvsn"),
        "prcs_stat_name": row.get("prcs_stat_name"),
    }, ensure_ascii=False))
