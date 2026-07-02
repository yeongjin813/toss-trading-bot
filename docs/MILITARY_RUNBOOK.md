# Military Service Runbook (Unattended Operation)

Short operator guide for checking and controlling the bot without reading the full codebase.  
**Default expectation:** KIS **VTS (paper/mock)** on EC2, strategy frozen, Telegram alerts on.

---

## 1. Is the bot alive?

**On EC2 (SSH):**

```bash
systemctl is-active toss-bot          # expect: active
tail -5 ~/toss-trading-bot/heartbeat.json
tail -20 ~/toss-trading-bot/project_metrics.log
```

**From Windows:**

```powershell
ssh -i "C:\path\to\tb.pem" ubuntu@YOUR_EC2_IP "systemctl is-active toss-bot"
```

**Telegram:** You should get EOD reports on trading days (if `USE_DAILY_TELEGRAM_REPORT=true`).  
**Healthcheck cron** (every 15 min) sends Telegram on CRITICAL/WARNING (service down, stale loop, disk full, etc.).

`heartbeat.json` fields to check:

| Field | Meaning |
|-------|---------|
| `last_loop_at` | Last successful RTH trading cycle |
| `last_broker_api_at` | Last broker sync / API activity |
| `last_reconcile_at` | Last portfolio reconciliation |
| `active_kill_switches` | Pause / no-buys / emergency flags |

---

## 2. Pause all trading

Edit `.env` on EC2:

```ini
TRADING_PAUSED=true
```

Then restart:

```bash
sudo systemctl restart toss-bot
```

Effect: **No new broker orders** (buys or sells). Reconciliation, logging, EOD reports, and healthcheck still run.

---

## 3. Disable new buys only (keep exits)

```ini
TRADING_PAUSED=false
ALLOW_NEW_BUYS=false
EMERGENCY_LIQUIDATE=false
```

Restart `toss-bot`. Existing stop-loss / sell signals can still execute.

---

## 4. Emergency liquidation

**Use only if you intentionally want to flatten all positions.**

```ini
EMERGENCY_LIQUIDATE=true
```

Restart during **US regular hours (09:30–16:00 ET)**. The bot will attempt to **SELL all holdings** once per cycle, then skip normal trading until you set `EMERGENCY_LIQUIDATE=false`.

You will get **CRITICAL Telegram** alerts when this mode runs.

---

## 5. Restart the service

```bash
cd ~/toss-trading-bot
# after .env changes:
sudo systemctl restart toss-bot
sudo systemctl status toss-bot --no-pager
```

---

## 6. Check recent logs

```bash
tail -100 ~/toss-trading-bot/project_metrics.log
grep -E 'GATE/KILL|EMERGENCY|RECONCILE|TRADE/FILLED|CRITICAL' ~/toss-trading-bot/project_metrics.log | tail -30
```

Download log to PC:

```powershell
scp -i "C:\path\to\tb.pem" ubuntu@YOUR_EC2_IP:~/toss-trading-bot/project_metrics.log .
```

---

## 7. Confirm Telegram

```bash
cd ~/toss-trading-bot
.venv/bin/python telegram_notifier.py --diagnose
```

Do **not** use `--demo` on production (sends fake trade samples).

---

## 8. Broker vs local state mismatch

Symptoms: `[RECONCILE/MISMATCH]` in logs, `holdings_mismatch` in `heartbeat.json`, wrong quantities in Telegram EOD.

**Safe steps:**

1. Confirm bot is `active` and not mid-order (`pending_order` in state).
2. Let one full RTH cycle complete (reconcile runs automatically).
3. If mismatch persists: set `TRADING_PAUSED=true`, restart, compare KIS app holdings vs `trading_state.json` → `_portfolio.broker_holdings`.
4. Do **not** hand-edit `trading_state.json` unless you know the correct broker qty.
5. Restore from backup if needed (see below).

---

## 9. Backups

Daily (optional cron):

```bash
.venv/bin/python scripts/daily_backup.py
```

Backups go to `~/toss-trading-bot/backups/YYYY-MM-DD/` (no `.env`).

**Restore `trading_state.json`:**

```bash
TRADING_PAUSED=true   # in .env first
cp backups/YYYY-MM-DD/trading_state.json ./trading_state.json
sudo systemctl restart toss-bot
```

**Do not commit to GitHub:** `.env`, `kis_token_cache.json`, `trading_state.json`, `backups/`, logs.

---

## 10. What NOT to do

- Do **not** switch to **live** real-money API while away.
- Do **not** `git pull` + restart on a schedule without reviewing changes.
- Do **not** change strategy params, `CAPITAL_AT_RISK`, or watchlist casually.
- Do **not** run `telegram_notifier.py --demo` on EC2.
- Do **not** ignore **KIS mock 90-day re-enrollment** or **annual API key renewal** (manual calendar reminders).

---

## Emergency checklist

| Step | Action |
|------|--------|
| 1 | `systemctl is-active toss-bot` |
| 2 | If runaway behavior suspected → `TRADING_PAUSED=true` + restart |
| 3 | If must flatten → `EMERGENCY_LIQUIDATE=true` + restart (RTH only) |
| 4 | Check `heartbeat.json` + last 50 log lines |
| 5 | Run `scripts/ec2_healthcheck.py --dry-run` |
| 6 | If KIS auth expired → renew keys / mock session, then restart |

---

See also: [OPERATIONS.md](OPERATIONS.md) (EC2 deploy, healthcheck cron, absence playbook).
