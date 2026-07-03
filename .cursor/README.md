# Cursor Automations (this repo)

Automations do **not** transfer across Cursor accounts. After switching accounts, recreate from the files below.

## Essential (recommended)

| File | Purpose |
|------|---------|
| `automation-essential.json` | Import / prefill template |
| `automation-essential-instructions.txt` | Paste into Instructions if the UI has no import |

**Triggers:** PR opened + pushed on `yeongjin813/toss-trading-bot`  
**Action:** PR comment (pytest + safety audit, report only)

**Not included (optional):** weekly/monthly cron audits — use `automation-trading-bot-absence.json` if you want those later.

## Setup

1. Cursor → **Automations** → **New**
2. Prefill from `automation-essential.json`, or paste `automation-essential-instructions.txt`
3. Connect GitHub for `yeongjin813/toss-trading-bot`
4. Save

Bot monitoring while away uses **EC2** `ec2_healthcheck` cron + Telegram — not Cursor Automations.
