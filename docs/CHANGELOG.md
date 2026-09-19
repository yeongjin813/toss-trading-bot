# Changelog

High-level release notes. Strategy research detail: [RESEARCH_LOG.md](RESEARCH_LOG.md).

---

## Tags

| Tag | Description |
|-----|-------------|
| `military-freeze-v1` | Frozen VTS config before military absence (Jun 2026): dual 70/30, Top4, kill switches, heartbeat, safety latch |

---

## 2026-09 (return from leave)

| Area | Change |
|------|--------|
| **Soft latch** | `eod_missing` / Telegram failures tracked but do not engage buy-block; hard latch kept for mismatch / broker stale / pending stuck |
| **VTS equity MTM** | When `broker_cash_usd=0`, equity = `CAPITAL_AT_RISK + unrealized(entry→mark)` so EOD curves can move |
| **Ops** | `scripts/compare_live_shadow.py`; `deploy/install-daily-backup-cron.sh`; mock 90-day re-enroll clean-slate notes in runbook |
| **Strategy** | Unchanged (prod freeze): Top4, 52w OFF, golden_cross OFF, 70/30 |

---

## 2026-06 (recent)

| Commit / area | Change |
|---------------|--------|
| **Unattended ops** | Kill switches, heartbeat, daily backup, military runbook |
| **Hardening** | Emergency liquidation confirm phrase; safety latch; live guard tests |
| **Phase 26** | Capital sweep; $100k VTS default |
| **Phase 25** | `USE_REGIME_GOLDEN_CROSS=false` |
| **Phase 22** | Top4 + hold band = 1 |
| **Phase 21** | 52w-high filter OFF |
| **Phase 19** | Dual 70/30 capital split |
| **Phase 15** | `watchlist_cycle.py` extract; atomic state writes |
| **Phase 14** | 25-ticker watchlist; sector caps |
| **Phase 8** | Split OPERATIONS.md; CI; logrotate |
| **Phase 6–7** | Fill monitor, Telegram, RTH-only polling |

---

## Documentation

| Date | Change |
|------|--------|
| 2026-09 | Return-from-leave ops notes; soft latch + VTS equity MTM documented |
| 2026-06 | README refactored to landing page; legacy content → `docs/REFERENCE.md` |

---

## How to compare to freeze

```bash
git diff military-freeze-v1..HEAD
git checkout military-freeze-v1   # read-only inspection
```
