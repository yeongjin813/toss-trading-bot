# Risk & Operational Safety

Residual risks and monitoring—not a guarantee of safe live trading.

---

## Core residual risks

### Backtest vs live parity

| Gap | Live | Backtest |
|-----|------|----------|
| ATR stop path | Intraday `session_low` (default) | Daily bar `Low` only |
| Fill timing | RTH limit during session | Next open (default) or close |
| VTS quirks | ccnl 500, zero USD cash field | Not modeled |

Mitigation: `USE_EOD_ATR_STOPS=true` for closer stop parity; `SLIPPAGE_BPS=5` and commission in backtests.

### Broker vs local state

- **Mitigated:** `PortfolioReconciliationEngine` (KIS `VTRP6504R`, ccnl fallback)
- **Residual:** Race during pending orders; manual trades outside bot
- **Response:** `[RECONCILE/MISMATCH]` logs; safety latch after repeated mismatch; see [MILITARY_RUNBOOK.md](MILITARY_RUNBOOK.md)

### Capital and sizing

- Dual-clamp uses **95%** deployable haircut
- `MAX_OPEN_POSITIONS`, `MAX_TICKER_EXPOSURE_USD`, `MAX_PORTFOLIO_USD` cap deployment
- Partial deployment (cash left over) is **expected**, not a bug

### Unattended operation

| Control | Purpose |
|---------|---------|
| `TRADING_PAUSED` | Block all orders |
| `ALLOW_NEW_BUYS=false` | Block new longs; allow exits |
| `EMERGENCY_LIQUIDATE` + confirm phrase | Flatten all (VTS test first) |
| `safety_latch.json` | Auto-block BUYs after repeated anomalies |
| `heartbeat.json` + healthcheck cron | Detect stale loop / API / pending orders |
| `scripts/daily_backup.py` | Local backup (copy offsite weekly) |

**Not automatic:** KIS mock 90-day re-enrollment, API key annual renewal, offsite backup.

---

## Live trading checklist

Before `KIS_ENVIRONMENT=live`:

- [ ] `KIS_LIVE_CONFIRMED=true`
- [ ] Telegram enabled with valid token/chat ID
- [ ] `CAPITAL_AT_RISK` **small** (e.g. $1k pilot—not $100k VTS default)
- [ ] Proportional limits: `MAX_PORTFOLIO_USD`, `MAX_TICKER_EXPOSURE_USD`, `MAX_DAILY_LOSS_USD`
- [ ] Read [OPERATIONS.md](OPERATIONS.md) and run full test suite
- [ ] Manual supervision plan—not for 18-month unattended live

---

## Monitoring

| Signal | Where |
|--------|--------|
| Trade fills | `trade_log.csv`, Telegram |
| Reconcile | `[RECONCILE]` in `project_metrics.log` |
| Kill switches | Startup banner, `heartbeat.json` → `active_kill_switches` |
| Health | `scripts/ec2_healthcheck.py`, systemd `toss-bot` |
| EOD | Telegram daily report after 16:00 ET |

Telegram failure does not send a Telegram alert—check logs and healthcheck; consider a second alert channel for long absences.

---

## Worst-case scenarios

1. **Flash move intraday** — live stop may fire; backtest may not  
2. **API outage during RTH** — cycle skip; alerts if healthcheck configured  
3. **Stale pending order** — latch + `PENDING_ORDER_CANCEL_MINUTES`  
4. **EC2 loss** — restore from offsite `backups/` copy  
5. **Accidental emergency env** — confirm phrase required (two-key)

Full technical write-up: [REFERENCE.md](REFERENCE.md) §§11–14.
