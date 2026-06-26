"""Probe KIS present-balance INQR_DVSN variants and full ccnl sell rows."""
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
for code in ("00", "01", "02", "03"):
    try:
        p = client.fetch_overseas_present_balance(natn_cd="840", inqr_dvsn_cd=code)
        rows = p.get("output1") or []
        nonzero = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = row.get("pdno")
            for k in ("cblc_qty13", "ovrs_stck_tot_qty", "ord_psbl_qty1", "ccld_qty_smtl1"):
                v = row.get(k)
                if v not in (None, "", "0", "0.00000000"):
                    nonzero.append((sym, k, v))
        print(f"INQR_DVSN_CD={code}: nonzero qty fields={len(nonzero)}")
        for item in nonzero[:5]:
            print(" ", item)
    except Exception as exc:
        print(f"INQR_DVSN_CD={code}: ERROR {exc}")

end = datetime.now()
start = end - timedelta(days=120)
rows = client.fetch_overseas_order_ccnl(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
sells = []
for row in rows:
    side = str(row.get("sll_buy_dvsn_cd") or row.get("sll_buy_dvsn") or "")
    if side in {"01", "1"}:
        sells.append(row)
print(f"\nccnl sells (120d): {len(sells)}")
for row in sells[-10:]:
    print(json.dumps({
        "pdno": row.get("pdno"),
        "ord_dt": row.get("ord_dt"),
        "odno": row.get("odno"),
        "sll_buy": row.get("sll_buy_dvsn_cd"),
        "ccld_qty": row.get("ccld_qty"),
        "ft_ord_qty": row.get("ft_ord_qty"),
    }, ensure_ascii=False))
