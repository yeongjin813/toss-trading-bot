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
14. [Phase 10 — Trend Hold & Exit Discipline](#phase-10--trend-hold--exit-discipline)
15. [Dual-Strategy Deployment Phases](#dual-strategy-deployment-phases)
16. [Phase 14 — Profit, Risk & Diversified Watchlist](#phase-14--profit-risk--diversified-watchlist)
17. [Phase 15 — Live-Loop Hardening](#phase-15--live-loop-hardening--pipeline-extract)
18. [Phases 21–25 — Research-Validated Prod Defaults](#phases-2125--research-validated-prod-defaults)

---

## Local Setup

### 1. Create `.env`

Copy from `.env.example` and fill in KIS credentials. Never commit `.env`.

**Production defaults (Phases 21–25)** — copy the full file from `.env.example`; key validated flags:

```ini
USE_SPY_MARKET_FILTER=true
USE_QQQ_REGIME_FILTER=true
USE_REGIME_GOLDEN_CROSS=false      # Phase 25: was true
USE_52W_HIGH_FILTER=false          # Phase 21: was true
USE_SCALE_IN=true
USE_WEEKLY_TREND_FILTER=true
USE_SCALE_OUT=true
REGIME_CAUTIOUS_MAX_POSITIONS=2
ENTRY_CONFIRMATION_DAYS=0
USE_VIX_REGIME_FILTER=false
SLIPPAGE_BPS=5

# Dual-strategy Phase 4: Legacy $70k + Top4 $30k on $100k total
DEPLOYMENT_PHASE=4
STRATEGY_MODE=dual
LEGACY_CAPITAL_PCT=70
TOP3_CAPITAL_PCT=30
TOP3_DRY_RUN_ENABLED=false
MOMENTUM_RANK_ENABLED=false
MOMENTUM_TOP_N=4                   # Phase 22: was 3
MOMENTUM_TOP_N_HOLD_BAND=1         # Phase 22: turnover band
MAX_DAILY_LOSS_USD=5000
MAX_OPEN_POSITIONS=5
MAX_TICKER_EXPOSURE_USD=25000
CAPITAL_AT_RISK=100000
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

### Live order timing (not close-only)

| | Live bot | Backtest (default) |
|---|---|---|
| **When orders fire** | **09:30–16:00 ET (RTH)** when signals pass gates — not only at 16:00 | Signal on bar **close**; fill at **next session open** (`BACKTEST_FILL_AT_NEXT_OPEN=true`) |
| **Poll interval** | 60s flat; **15s** when holding or pending order | N/A (simulated daily bars) |
| **Order type** | KIS **limit** (`KIS_ORDER_TYPE=limit`) with price buffer | Open/close + commission + slippage |
| **Signal basis** | Daily indicators; **forming bar** updates intraday | Completed daily bars only |
| **New BUY** | RTH; blocked in open/close volatility windows | Next-bar open after crossover signal |
| **ATR stop** | Intraday `session_low` scan (default); `USE_EOD_ATR_STOPS=true` for backtest parity | Daily bar low (unless EOD mode aligned) |

**Takeaway:** production is **intraday RTH limit trading on a daily-bar strategy** — not a “market-on-close only” bot. Backtest timing is intentionally conservative (signal → next open).

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
.venv/bin/pip install -r requirements.txt -q
.venv/bin/python scripts/sync_phase14_env.py
```

**Verify prod keys synced** (compare with `.env.example`):

```bash
grep -E '^(USE_REGIME_GOLDEN_CROSS|USE_52W_HIGH_FILTER|MOMENTUM_TOP_N|MOMENTUM_TOP_N_HOLD_BAND|LEGACY_CAPITAL_PCT|TOP3_CAPITAL_PCT|DEPLOYMENT_PHASE|STRATEGY_MODE|ENTRY_CONFIRMATION_DAYS|USE_VIX_REGIME_FILTER|SLIPPAGE_BPS)=' .env
```

Expected: `USE_REGIME_GOLDEN_CROSS=false`, `USE_52W_HIGH_FILTER=false`, `MOMENTUM_TOP_N=4`, `MOMENTUM_TOP_N_HOLD_BAND=1`.

Then restart:

```bash
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
| KIS mock app “orderable USD” | ~$100,000 | Real sandbox buying power (hard ceiling for VTS) |
| `.env` `CAPITAL_AT_RISK` | `100000` | Strategy cap used for sizing (prod validated, Phase 26) |
| Log `Deployable Cash` | matches `CAPITAL_AT_RISK` | What the bot deploys |
| Log `broker_cash_usd = 0` | VTS API gap | **Not** broke — bot falls back to `CAPITAL_AT_RISK` |

**Prod VTS (Phase 26 adopted):** use full sandbox buying power with proportional risk limits:

```env
CAPITAL_AT_RISK=100000
MAX_TICKER_EXPOSURE_USD=25000
MAX_DAILY_LOSS_USD=5000
MAX_PORTFOLIO_USD=100000
```

**Live account scale-up** (when off VTS): multiply all USD caps by the same factor. Example $250k:

```env
CAPITAL_AT_RISK=250000
MAX_TICKER_EXPOSURE_USD=62500
MAX_DAILY_LOSS_USD=12500
MAX_PORTFOLIO_USD=250000
```

Re-run `python scripts/capital_sweep.py` before raising capital. Do not exceed broker orderable USD.

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
```

On EC2: `.venv/bin/python telegram_notifier.py --diagnose`

**Do not use** `telegram_notifier.py --verbose` alone for prod checks — it used to send **fake** sample trades. Use `--demo` only when you intentionally want sample messages:

```bash
python telegram_notifier.py --demo
```

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

**Message types (monitor only — cannot place orders via Telegram):**

| Type | When | RTH gate |
|---|---|---|
| Trade report | Legacy fill `FILLED` / `PARTIAL` | Any time fill occurs |
| CRITICAL | Crash, auth fail, startup validation | Always (duplicate throttle: `TELEGRAM_ALERT_THROTTLE_SECONDS`) |
| WARNING | KIS 500, reconcile error, timeouts | Telegram **only during RTH** |
| INFO | Top3 shadow rebalance, over-deploy trim queued/submitted | Any time |
| EOD report | After 16:00 ET, once per NY session | `USE_DAILY_TELEGRAM_REPORT` |

Diagnose: `python telegram_notifier.py --diagnose` (EC2: `.venv/bin/python telegram_notifier.py --diagnose`).

**Related capital env (not Telegram-specific but drives INFO trim alerts):** `MAX_PORTFOLIO_USD`, `OVERDEPLOYMENT_TRIM_ENABLED`, `OVERDEPLOYMENT_TRIM_TARGET_PCT` — see [Phase 13 in README](../README.md#phase-13-broker-holdings-sync--report-parity-2026-06).

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

Growth: ~15–25 MB per US trading day (60s × 25 tickers during RTH). `trade_log.csv` grows slowly (one row per fill).

### Automated rotation (recommended on EC2)

Repo ships `deploy/logrotate-toss-bot.conf`:

| File | Policy | Retention |
|---|---|---|
| `project_metrics.log` | weekly **or** 50 MB | 8 rotated files (compressed) |
| `trade_log.csv` | monthly | 12 months (compressed) |

Uses `copytruncate` so `toss-bot` systemd does not need a restart.

**Install on EC2** (after `git pull`):

```bash
cd ~/toss-trading-bot
git pull origin main
sudo bash deploy/install-logrotate.sh
```

Custom install path:

```bash
BOT_DIR=/home/ubuntu/toss-trading-bot sudo -E bash deploy/install-logrotate.sh
```

Verify dry-run:

```bash
sudo logrotate -d /etc/logrotate.d/toss-bot
```

Force one rotation (optional):

```bash
sudo logrotate -f /etc/logrotate.d/toss-bot
```

### Manual truncate (fallback)

```bash
cd ~/toss-trading-bot
sudo cp project_metrics.log project_metrics.log.bak
sudo truncate -s 0 project_metrics.log
```

---

## Absence Playbook (long leave / unattended prod)

**Policy:** freeze EC2 code and `.env` — **no automatic `git pull`** while away. Real-time alerts come from **EC2 cron**, not Cursor Automation.

| Layer | What runs | Auto-fix? |
|---|---|---|
| **EC2 healthcheck** | Every 15 min — `scripts/ec2_healthcheck.py` | Alerts only (Telegram) |
| **systemd** | `Restart=always` on `toss-bot` | Restarts process |
| **logrotate** | `deploy/logrotate-toss-bot.conf` | Rotates logs |
| **Cursor Automation** | PR guard + weekly/monthly **read-only** audit | **Never** changes strategy |

### Install healthcheck cron (EC2)

```bash
cd ~/toss-trading-bot
git pull origin main
bash deploy/install-healthcheck-cron.sh
.venv/bin/python scripts/ec2_healthcheck.py --dry-run
```

Checks: `toss-bot` active, root disk ≥ 80%, `project_metrics.log` ≥ 400 MB.  
Repeats the same Telegram alert at most once per 4 hours per issue.

Optional `.env` overrides:

```env
HEALTHCHECK_DISK_WARN_PCT=80
HEALTHCHECK_LOG_WARN_MB=400
HEALTHCHECK_ALERT_COOLDOWN_SEC=14400
```

### Cursor Automation

Import draft: `.cursor/automation-trading-bot-absence.json` (PR guard + weekly/monthly read-only).  
Does **not** SSH to EC2 or modify prod — use healthcheck cron for outages.

### Before you leave

1. `systemctl is-active toss-bot` → `active`
2. `sudo logrotate -d /etc/logrotate.d/toss-bot`
3. `bash deploy/install-healthcheck-cron.sh`
4. Backup EC2 `.env` locally (never commit)
5. Confirm Telegram: `.venv/bin/python telegram_notifier.py --diagnose` (not `--demo`)

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
| Momentum ranking mode | `MOMENTUM_RANKING_MODE=legacy` (hardcoded in `main.py`) | **Production always Legacy.** Enhanced is research-only — see README Phase 16 |
| Loop cooldown | `LOOP_COOLDOWN_SECONDS`, `LOOP_COOLDOWN_HELD_SECONDS` | 60s flat; **15s when holding or pending order** (faster ATR stop checks) |
| KIS environment | `KIS_ENVIRONMENT=vts`, `KIS_LIVE_CONFIRMED` | Live API blocked without explicit confirmation; URL/label mismatch fails at startup |
| QQQ half-size regime | `USE_QQQ_REGIME_FILTER=true` | Half position when SPY &lt; 200MA and QQQ &gt; 200MA |
| Fill sync fallback | `execution_engine.py` | On ccnl/nccs HTTP 500, infer fill from `present-balance` |
| Walk-forward benchmarks | `python run_backtest.py --walk-forward --yfinance` | Strat vs equal-weight B&H vs SPY alpha table |
| Rolling OOS Top3 | `python scripts/walk_forward_momentum.py --yfinance` | Train 3y → test 1y; legacy equal vs inv-vol vs enhanced |
| Dry-run | `KIS_DRY_RUN=true` | Simulate instant fills; logs `DRY_RUN` in `trade_log.csv` |
| EOD report | `USE_DAILY_TELEGRAM_REPORT=true` | Telegram summary after 16:00 ET once per session |

**Production EC2 defaults (Phase 1 — legacy only):**

```ini
DEPLOYMENT_PHASE=1
STRATEGY_MODE=legacy
MOMENTUM_RANK_ENABLED=false
MOMENTUM_TOP_N=3
USE_QQQ_REGIME_FILTER=true
KIS_DRY_RUN=false
USE_DAILY_TELEGRAM_REPORT=true
```

**Safe signal testing on EC2:** set `KIS_DRY_RUN=true`, restart `toss-bot`, confirm startup banner shows `*** KIS DRY-RUN MODE ***`, then set back to `false` before real orders.

Full architecture: [README Live System Flow](../README.md#live-system-flow) · [Phase 9](../README.md#phase-9-momentum-universe-strategy-overhaul--ops-polish-2026-06).

---

## Dual-Strategy Deployment Phases

Four-phase rollout separating the **legacy signal engine** (breakout + ATR trail + regime exits) from the **Top3 momentum rebalance** strategy (equal-weight Top-N, Friday rebalance, exit when dropped).

| Phase | Goal | `.env` keys | Live | Backtest |
|---|---|---|---|---|
| **1** | Operate improved legacy only | `DEPLOYMENT_PHASE=1`, `STRATEGY_MODE=legacy`, `MOMENTUM_RANK_ENABLED=false` | Legacy engine | `python run_backtest.py --yfinance` |
| **2** | Compare Top3 offline | `TOP3_BACKTEST_ONLY=true` (optional flag) | Unchanged | `python run_backtest.py --strategy compare --yfinance` |
| **3** | Parallel shadow | `DEPLOYMENT_PHASE=3`, `STRATEGY_MODE=dual`, `TOP3_DRY_RUN_ENABLED=true` | Legacy + Top3 shadow logs/Telegram | Re-run compare before enabling |
| **4** | Live capital split | `DEPLOYMENT_PHASE=4`, `STRATEGY_MODE=dual`, `LEGACY_CAPITAL_PCT=70`, `TOP3_CAPITAL_PCT=30` | 70/30 sizing; Top3 orders via KIS (respects `KIS_DRY_RUN`) | Monitor live vs shadow |

**Phase 2 backtest examples:**

```powershell
# 1-year window (approx)
python run_backtest.py --strategy compare --yfinance --start 2025-06-01

# 2024–2026 window
python run_backtest.py --strategy compare --yfinance --start 2024-01-01 --end 2026-06-01
```

**Advance checklist (2 → 4):**

1. Phase 2: Top3 total return beats legacy on same window (1Y and 2024–2026).
2. Phase 4 (production): Set env below; verify startup shows `capital=70/30`, `top3=live-split`, Legacy **$70,000** + Top3 **$30,000** on **$100,000** total.

**Production EC2 defaults (Phase 4 + Phases 21–25 — $100k total):**

```ini
CAPITAL_AT_RISK=100000
DEPLOYMENT_PHASE=4
STRATEGY_MODE=dual
LEGACY_CAPITAL_PCT=70
TOP3_CAPITAL_PCT=30
TOP3_DRY_RUN_ENABLED=false
MOMENTUM_RANK_ENABLED=false
MOMENTUM_TOP_N=4
MOMENTUM_TOP_N_HOLD_BAND=1
USE_REGIME_GOLDEN_CROSS=false
USE_52W_HIGH_FILTER=false
USE_SCALE_IN=true
MAX_DAILY_LOSS_USD=5000
MAX_TICKER_EXPOSURE_USD=25000
MAX_OPEN_POSITIONS=5
OVERDEPLOYMENT_TRIM_ENABLED=true
OVERDEPLOYMENT_TRIM_TARGET_PCT=0.98
USE_QQQ_REGIME_FILTER=true
KIS_DRY_RUN=false
USE_DAILY_TELEGRAM_REPORT=true
```

**Limitations:** KIS VTS does not tag orders by strategy. Top3 shadow state lives in `trading_state.json` → `_portfolio._top3_shadow`. Phase 4 uses capital-slice sizing; broker positions are shared — reconcile carefully before advancing.

**Over-deployment recovery:** When marked holdings exceed `CAPITAL_AT_RISK`, set `OVERDEPLOYMENT_TRIM_ENABLED=true`. At RTH the bot sells from the largest positions once per day until deployable cash recovers (`[TRIM]` / `[RECONCILE/CAP]` in logs). Manual KIS trim is still valid if you prefer immediate control.

---

## Phase 10 — Trend Hold & Exit Discipline

| Feature | Env / file | What it does |
|---|---|---|
| Breakout-only entry | `config.py` | All regimes: 20-day high breakout (`entry_mode=breakout`) |
| Min-hold soft exits | `config.py` + `analytics.py` | `min_hold_days=5` — blocks trail/trend/RSI/crossover for first 5 bars held |
| Wider hard stop | `config.py` | `stop_loss_pct=0.08` (−8%) + 2× ATR (always fires, ignores min-hold) |
| Regime trend exits | `config.py` | MEGA/DEFAULT: 5-day below 50MA; HIGH_BETA/MOMENTUM: off + skip when momentum-ranked |
| Profit trails | `config.py` | MEGA 18%/15%; HIGH_BETA 20%/15%; MOMENTUM 25%/18% |
| Top-3 momentum | `MOMENTUM_TOP_N=3` | Concentrated capital on highest-scoring names |
| Exit telemetry | `portfolio_backtest.py` | Backtest summary prints SELL counts by `exit_reason` |

**Production EC2 defaults (Phase 4 — supersede Phase 1 block below):**

See [Dual-Strategy Deployment Phases](#dual-strategy-deployment-phases) for full Phase 4 `.env`.

**Legacy-only reference (Phase 1 rollback):**

```ini
DEPLOYMENT_PHASE=1
STRATEGY_MODE=legacy
MOMENTUM_RANK_ENABLED=false
MOMENTUM_TOP_N=3
USE_QQQ_REGIME_FILTER=true
KIS_DRY_RUN=false
USE_DAILY_TELEGRAM_REPORT=true
```

**Walk-forward validation (legacy engine, no Top-N overlay):**

```powershell
python run_backtest.py --walk-forward --yfinance --momentum-top-n 3
```

Full strategy matrix: [README Section 8](../README.md#8-strategy-configurations--parameters) · [Phase 10](../README.md#phase-10-trend-hold--exit-discipline-2026-06).

---

## Phase 14 — Profit, Risk & Diversified Watchlist

Phase 14 closes the gap between live, backtest, and production risk posture. Full architecture: [README Phase 14](../README.md#phase-14-profit-risk-parity--diversified-universe-2026-06) · [Live System Flow](../README.md#live-system-flow).

| Feature | Env / file | What it does |
|---|---|---|
| 25-ticker universe | `WATCHLIST`, `market_registry.py` | 15 tech core + 10 non-tech (healthcare, finance, energy, consumer, industrial) |
| Sector caps | `MAX_POSITIONS_PER_SECTOR`, `TICKER_SECTORS` | Blocks new BUY when sector is full |
| Top3 sector diversify | `MOMENTUM_SECTOR_DIVERSIFY`, `MOMENTUM_MAX_PER_SECTOR` | At most one Top3 name per sector |
| Legacy/Top3 ownership | `strategy_ownership.py` | Prevents both strategies opening the same ticker |
| Golden-cross regime | `USE_REGIME_GOLDEN_CROSS` | Cautious / risk-off caps when SPY 50MA ≤ 200MA |
| Vol-adjusted risk | `USE_VOL_ADJUSTED_RISK`, `VOL_TARGET_PCT` | Scales per-trade risk from SPY ATR% |
| Entry filters | `USE_WEEKLY_TREND_FILTER`, `USE_52W_HIGH_FILTER` | Shared live ↔ backtest via `trading_features.py` |
| Scale-in / scale-out | `USE_SCALE_IN`, `USE_SCALE_OUT` | Partial entry + partial profit exit |
| Stale limit cancel | `PENDING_ORDER_CANCEL_MINUTES` | Cancel unfilled limits after 45 min (default) |
| Consecutive loss CB | `MAX_CONSECUTIVE_LOSS_DAYS` | Blocks new BUY after N losing days |

**Production EC2 `.env` (Phase 4 + Phase 14):** use the [Local Setup](#local-setup) block — 25-ticker `WATCHLIST` and Phase 14 flags are included.

**Verify after deploy:**

```bash
.venv/bin/python -m pytest test_market_regime.py test_strategy_ownership.py test_entry_filters.py test_market_registry.py -q
grep -E 'WATCHLIST|Phase|capital=|top3=' project_metrics.log | tail -20
```

**Watchlist A/B (optional, on PC):**

```powershell
python scripts/compare_watchlist.py
```

---

## Phase 15 — Live-Loop Hardening & Pipeline Extract

Architecture cleanup for production stability. Full detail: [README Phase 15](../README.md#phase-15-live-loop-hardening--pipeline-extract-2026-06).

| Component | File | Role |
|---|---|---|
| RTH orchestration | `watchlist_cycle.py` | reconcile → trim → fills → momentum → legacy → Top3 |
| State I/O | `state_persistence.py` | Atomic `trading_state.json` writes |
| Ticker signals + orders | `main.py` | `process_ticker`, KIS dispatch (injected into cycle via `WatchlistCycleDeps`) |

**After `git pull` on EC2:** merge any new `.env` keys from `.env.example` (Phase 17: `SLIPPAGE_BPS`, confirm VIX/entry-confirm OFF). Restart `toss-bot`.

**Verify:**

```bash
.venv/bin/python -m pytest test_state_persistence.py test_watchlist_cycle.py -q
systemctl is-active toss-bot
grep -E '\[CYCLE\]|\[RECONCILE\]|Cycle [0-9]+ started' project_metrics.log | tail -10
```

---

## Live OHLCV Cache Hardening (2026-06)

Three production risks addressed in `market_data_cache.py`:

| Risk | Fix |
|---|---|
| **Intraday CSV pollution** | RTH forming bar kept in memory only; disk stores **completed EOD bars**; startup heals polluted CSV tails |
| **15-ticker sequential skew** | `PARALLEL_TICKER_REFRESH=true` — optional parallel refresh at cycle start (VTS: keep `false`) |
| **Pandas memory fragmentation** | In-place row updates during RTH; `pd.concat` only on new session dates; periodic `gc.collect()` |

**Env:**

```ini
PARALLEL_TICKER_REFRESH=true
TICKER_REFRESH_WORKERS=8
CACHE_GC_EVERY_N_CYCLES=60
```

Post-RTH the bot calls `finalize_intraday_bars()` before sleeping — commits forming bars to CSV once per session.

---

## Phases 21–25 — Research-Validated Prod Defaults

Summary of backtest-driven changes applied to `.env.example` and EC2 (June 2026). Full detail: [README Phases 21–25](../README.md#phase-21-entry-filter-ablation-2026-06).

| Phase | Change | Effect | Adopted |
|---|---|---|---|
| 21 | `USE_52W_HIGH_FILTER=false` | +2.5pp CAGR | Yes |
| 22 | `MOMENTUM_TOP_N=4`, `MOMENTUM_TOP_N_HOLD_BAND=1` | +0.6pp CAGR, -88 trades | Yes |
| 23 | OOS validation (2023–25) | No overfitting | Confirmed |
| 24 | Aggressive vs prod | Config B fails bear2022; Config C gain = golden_cross | Partial |
| 25 | `USE_REGIME_GOLDEN_CROSS=false` | +0.66pp CAGR, bear2022 unchanged | Yes |

**EC2 deploy one-liner** (after `git push`):

```bash
cd ~/toss-trading-bot && git fetch origin && git reset --hard origin/main && .venv/bin/pip install -r requirements.txt -q && .venv/bin/python scripts/sync_phase14_env.py && sudo systemctl restart toss-bot && systemctl is-active toss-bot
```

**Verify after deploy:**

```bash
grep -E '^(USE_REGIME_GOLDEN_CROSS|USE_52W_HIGH_FILTER|MOMENTUM_TOP_N|MOMENTUM_TOP_N_HOLD_BAND)=' .env
git log -1 --oneline
```

**Research scripts** (local only):

```powershell
python scripts/entry_filter_ablation.py
python scripts/oos_validation.py
python scripts/golden_cross_ablation.py
python scripts/aggressive_sweep.py
```

---

```powershell
python test_analytics.py
python test_execution_engine.py
python test_momentum_ranker.py
python test_backtest_benchmarks.py
python test_daily_report.py
python test_telegram_notifier.py
python test_market_data_cache.py
python test_top3_backtest.py
```
