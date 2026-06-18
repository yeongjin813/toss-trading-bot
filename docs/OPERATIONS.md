# Production Operations Guide

Day-to-day guide for running the KIS VTS live bot locally or on AWS EC2.  
For strategy math, architecture, and backtest theory, see the [main README](../README.md).

**VTS base URL:** `https://openapivts.koreainvestment.com:29443`

---

## Table of Contents

1. [Local Setup](#local-setup)
2. [When the Bot Runs](#when-the-bot-runs)
3. [Monitor & Control](#monitor--control)
4. [Deploy to EC2](#deploy-to-ec2)
5. [Capital Model](#capital-model)
6. [Telegram Alerts](#telegram-alerts)
7. [Phase 6 — Fills & Risk](#phase-6--fills--risk)
8. [Phase 7 — Telegram Integration](#phase-7--telegram-integration)
9. [Phase 7.1 — RTH-Only Polling](#phase-71--rth-only-polling)
10. [Log Management](#log-management)
11. [Troubleshooting](#troubleshooting)
12. [Phase 8 — Hardening](#phase-8--hardening)
13. [Phase 9 — Momentum, Dry-Run & EOD Report](#phase-9--momentum-dry-run--eod-report)

---

## Local Setup

### 1. Create `.env`

Copy from `.env.example` and fill in KIS credentials. Never commit `.env`.

```ini
KIS_APP_KEY=your_app_key_here
KIS_APP_SECRET=your_app_secret_here
KIS_CANO=your_account_number
KIS_ACNT_PRDT_CD=01

WATCHLIST=AAPL,MSFT,NVDA,META,AMZN,GOOGL,TSLA,AMD,AVGO,NFLX,PLTR,CRWD,TSM,SHOP,UBER
USE_SPY_MARKET_FILTER=false
USE_QQQ_REGIME_FILTER=true
CAPITAL_AT_RISK=100000
RISK_PER_TRADE=0.01
LOOP_COOLDOWN_SECONDS=60
KIS_REQUEST_TIMEOUT_SECONDS=30
KIS_SLOW_API_MS=3000
KIS_ORDER_MAX_RETRIES=3
KIS_ORDER_RETRY_BACKOFF_SECONDS=2.0
USE_EOD_ATR_STOPS=false
KIS_ORDER_TYPE=limit

# Momentum Top-N (new BUY only)
MOMENTUM_RANK_ENABLED=true
MOMENTUM_TOP_N=5

# Safety & reporting
KIS_DRY_RUN=false
USE_DAILY_TELEGRAM_REPORT=true

USE_TELEGRAM_ALERTS=false
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

Full variable reference: [README Appendix C](../README.md#appendix-c-configuration-externalization-matrix).

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Run

```powershell
python main.py
```

Stop with `Ctrl+C`. State persists to `trading_state.json`.

---

## When the Bot Runs

| Calendar | NY time (ET) | Behavior | KIS API |
|---|---|---|---|
| Weekend / holiday | any | Sleep `MARKET_CLOSED_SLEEP_SECONDS` (3600s) | **None** |
| Weekday | Before 09:30 | Sleep until RTH (wake ≤ every 3600s) | **None** |
| Weekday | 09:30–16:00 (RTH) | Watchlist cycle every 60s | **Active** |
| Weekday | After 16:00 | EOD Telegram report (once), then sleep until next RTH | **None** (report only) |

See [Live System Flow](../README.md#live-system-flow) for the full gate and fill pipeline.

**Log tags**

| Tag | Meaning |
|---|---|
| `[GATE] US market closed` | Weekend/holiday — long sleep |
| `[GATE/RTH] ... no KIS API calls` | Weekday off-hours — no polling |
| `--- Cycle N started ---` | RTH active cycle |

**RTH pipeline (inside regular hours only)**

```
reconcile → fill poll → RTH/risk gates → dispatch → fill poll → trade_log.csv
```

---

## Monitor & Control

### Check service status

| Where | Command |
|---|---|
| **On EC2** (`ubuntu@ip-...`) | `systemctl is-active toss-bot` |
| **Windows PowerShell** | `ssh -i "C:\path\to\tb.pem" ubuntu@YOUR_EC2_IP "systemctl is-active toss-bot"` |

Expected: `active`

> If your shell prompt is already `ubuntu@ip-...`, you are on the server. Run `systemctl` directly — do **not** run `ssh` with a Windows `C:\...` key path from inside EC2.

### Tail logs

```bash
# EC2
tail -f ~/toss-trading-bot/project_metrics.log
grep RECONCILE ~/toss-trading-bot/project_metrics.log | tail -5
```

```powershell
ssh -i "C:\path\to\tb.pem" ubuntu@YOUR_EC2_IP "tail -f ~/toss-trading-bot/project_metrics.log"
```

### Download log to PC

```powershell
scp -i "C:\path\to\tb.pem" ubuntu@YOUR_EC2_IP:~/toss-trading-bot/project_metrics.log "C:\path\to\project_metrics_aws.log"
```

---

## Deploy to EC2

### First-time server layout

```
/home/ubuntu/toss-trading-bot/
├── .venv/
├── .env                 # never commit
├── main.py
├── project_metrics.log
└── trading_state.json
```

Create venv once:

```bash
cd ~/toss-trading-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### systemd unit

File: `/etc/systemd/system/toss-bot.service`

```ini
[Unit]
Description=Toss Trading Bot (KIS VTS)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/toss-trading-bot
Environment=PATH=/home/ubuntu/toss-trading-bot/.venv/bin
ExecStart=/home/ubuntu/toss-trading-bot/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable toss-bot
sudo systemctl start toss-bot
sudo systemctl status toss-bot
```

### Update after `git push`

```bash
cd ~/toss-trading-bot
git fetch origin && git reset --hard origin/main
.venv/bin/pip install -r requirements.txt
sudo systemctl restart toss-bot
systemctl is-active toss-bot
```

> **Ubuntu PEP 668:** Use `.venv/bin/pip`, not system `pip`.

> **git pull blocked by `data/*.csv`:** `git fetch origin && git reset --hard origin/main`

`.env` on EC2 is separate from your PC — copy or edit on the server.

---

## Capital Model

| Source | Typical value | Meaning |
|---|---|---|
| KIS mock app “orderable USD” | ~$100,000 | Real sandbox buying power |
| `.env` `CAPITAL_AT_RISK` | e.g. `100000` | Strategy cap used for sizing |
| Log `Deployable Cash` | matches `CAPITAL_AT_RISK` | What the bot deploys |
| Log `broker_cash_usd = 0` | VTS API gap | **Not** broke — bot falls back to `CAPITAL_AT_RISK` |

Align bot sizing with your mock account:

```env
CAPITAL_AT_RISK=100000
MAX_TICKER_EXPOSURE_USD=10000
```

---

## Telegram Alerts

### Setup

1. Create bot via [@BotFather](https://t.me/BotFather) → `TELEGRAM_BOT_TOKEN`
2. Open **your** bot → `/start`
3. Set `TELEGRAM_CHAT_ID` (from [@userinfobot](https://t.me/userinfobot) or `--diagnose`)
4. `USE_TELEGRAM_ALERTS=true` in `.env`

### Verify

```bash
python telegram_notifier.py --diagnose
python telegram_notifier.py --verbose
```

On EC2: `.venv/bin/python telegram_notifier.py --diagnose`

### What to expect

| Alert | Telegram? | When |
|---|---|---|
| Trade fill BUY/SELL | Yes | Confirmed fill during RTH |
| WARNING timeout | **RTH only** | Off-hours → log only |
| CRITICAL reconcile `500` | Yes | Transient VTS error |
| CRITICAL crash / auth | Yes | Investigate immediately |

---

## Phase 6 — Fills & Risk

- Orders accepted (`rt_cd=0`) do **not** update `held_quantity` until fills confirm.
- `OrderFillMonitor` polls KIS ccnl/nccs (`VTTS3035R` / `VTTS3018R`).
- Events append to `trade_log.csv`.
- `RiskGuard`: `MAX_DAILY_LOSS_USD`, `MAX_OPEN_POSITIONS`, `MAX_TICKER_EXPOSURE_USD`.
- **VTS fallback (Phase 9):** when ccnl/nccs return HTTP 500, bot infers fill from `present-balance` — look for `[FILL/BROKER-FB]` in logs.

**Trade log statuses:** `ACCEPTED`, `PARTIAL`, `FILLED`, `REJECTED`, `RELEASED`, `DRY_RUN`

---

## Phase 7 — Telegram Integration

- Module: `telegram_notifier.py`
- Hooks: `main.py` fill callbacks + exception handlers
- Each message uses `async with Bot(token) as bot:` (v21+ session lifecycle)
- Gated by `USE_TELEGRAM_ALERTS`

---

## Phase 7.1 — RTH-Only Polling

Commit `46767be` — reduces off-hours VTS noise and Telegram spam.

| Change | File |
|---|---|
| Skip watchlist outside RTH | `main.py` |
| `seconds_until_us_rth_open()` | `analytics.py` |
| WARNING Telegram off-hours | `main.py` |
| HTTP timeout 30s default | `KIS_REQUEST_TIMEOUT_SECONDS` |

---

## Log Management

Growth: ~10–15 MB per US trading day (60s × 15 tickers during RTH).

Rotate on EC2:

```bash
cd ~/toss-trading-bot
sudo cp project_metrics.log project_metrics.log.bak
sudo truncate -s 0 project_metrics.log
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Permission denied (publickey)` | Wrong PEM path, or running `ssh` from inside EC2 |
| `externally-managed-environment` | `.venv/bin/pip install -r requirements.txt` |
| `Chat not found` (Telegram) | `/start` the exact bot matching your token; run `--diagnose` |
| `broker_cash_usd = 0` | Normal VTS parse gap — check `Deployable Cash` instead |
| CRITICAL reconcile `500` | Transient KIS error — bot continues; retry next RTH cycle |
| All HOLD, no trades | Normal — liquidity/signal gates; not necessarily a bug |
| Alerts on PC but not EC2 | Add Telegram vars to server `.env` and restart |
| ccnl/nccs HTTP 500 + pending lock | Bot retries + broker holdings fallback; check `[FILL/BROKER-FB]` logs |
| TSLA filled but state stuck | Run reconcile at startup; verify `held_quantity` in `trading_state.json` |
| Test signals without broker risk | Set `KIS_DRY_RUN=true` — instant local fills, no KIS order API |

VTS incident history: [README Appendix A](../README.md#appendix-a-infrastructure-patch-ledger-vts-mock-api-bypasses).

---

## Phase 8 — Hardening

| Feature | Env / file | What it does |
|---|---|---|
| NYSE calendar merge | `holidays` package | Adds Good Friday etc.; logs `[CALENDAR]` drift |
| API latency | `KIS_SLOW_API_MS` | `[KIS/HTTP] … api_response_time=…ms` on every KIS call |
| Order retry | `KIS_ORDER_MAX_RETRIES`, `order_retry_queue.json` | Inline retry + next-cycle queue for transient failures |
| Backtest ATR parity | `USE_EOD_ATR_STOPS=true` | Daily bar low stops (matches backtest); default keeps intraday stops |
| CI | `.github/workflows/ci.yml` | Runs all `test_*.py` on push/PR |

Full step-by-step changelog: [README Phase 8](../README.md#phase-8-production-hardening--readme-improvement-log).

---

## Phase 9 — Momentum, Dry-Run & EOD Report

| Feature | Env / file | What it does |
|---|---|---|
| Momentum Top-N | `MOMENTUM_RANK_ENABLED`, `MOMENTUM_TOP_N` | Friday rebalance; only Top-N tickers get new BUY (exits always run) |
| QQQ half-size regime | `USE_QQQ_REGIME_FILTER=true` | Half position when SPY &lt; 200MA and QQQ &gt; 200MA |
| Fill sync fallback | `execution_engine.py` | On ccnl/nccs HTTP 500, infer fill from `present-balance` |
| Walk-forward benchmarks | `python run_backtest.py --walk-forward --yfinance` | Strat vs equal-weight B&H vs SPY alpha table |
| Dry-run | `KIS_DRY_RUN=true` | Simulate instant fills; logs `DRY_RUN` in `trade_log.csv` |
| EOD report | `USE_DAILY_TELEGRAM_REPORT=true` | Telegram summary after 16:00 ET once per session |

**Production EC2 defaults (live trading):**

```ini
MOMENTUM_RANK_ENABLED=true
MOMENTUM_TOP_N=5
USE_QQQ_REGIME_FILTER=true
KIS_DRY_RUN=false
USE_DAILY_TELEGRAM_REPORT=true
```

**Safe signal testing on EC2:** set `KIS_DRY_RUN=true`, restart `toss-bot`, confirm startup banner shows `*** KIS DRY-RUN MODE ***`, then set back to `false` before real orders.

Full architecture: [README Live System Flow](../README.md#live-system-flow) · [Phase 9](../README.md#phase-9-momentum-universe-strategy-overhaul--ops-polish-2026-06).

---

## Verification Tests

```powershell
python test_analytics.py
python test_execution_engine.py
python test_momentum_ranker.py
python test_backtest_benchmarks.py
python test_daily_report.py
python test_telegram_notifier.py
```
