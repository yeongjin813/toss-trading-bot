# Cursor Automations (this repo)

**Important:** Files here are templates only. Saving to Git does **not** activate Automations on your Cursor account. You must create it in the Cursor UI and click **Save**.

## Prerequisites (if "doesn't work", check these first)

1. **Cursor Pro or Business** — Automations are not on the Free plan.
2. **GitHub connected to Cursor** — [cursor.com/dashboard](https://cursor.com/dashboard) → Integrations → install **Cursor GitHub App** → grant access to `yeongjin813/toss-trading-bot`.
3. Create at **[cursor.com/automations](https://cursor.com/automations)** or **Agents window → Automations → New**.

## Essential automation (recommended)

**Name:** `Trading Bot PR Guard (Essential)`

| Setting | Value |
|---------|--------|
| **Trigger 1** | GitHub → Pull request **opened** → repo `yeongjin813/toss-trading-bot` |
| **Trigger 2** | GitHub → Pull request **pushed** → same repo |
| **Tool** | **Comment on Pull Request** (do not enable auto-approve) |
| **Repository** | `yeongjin813/toss-trading-bot` / branch `main` |
| **Instructions** | Copy all of `automation-essential-instructions.txt` |

Then click **Save** and turn the automation **On**.

## Copy-paste instructions

Open `automation-essential-instructions.txt` in this folder and paste the full text into the automation **Instructions** field.

## What we deliberately skip

| Skipped | Why |
|---------|-----|
| Weekly/monthly cron | Not needed while prod is frozen |
| EC2 monitoring | Already on EC2 via `scripts/ec2_healthcheck.py` cron + Telegram |

Optional full version (with cron): `automation-trading-bot-absence.json`

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No Automations menu | Upgrade to Pro/Business |
| Can't pick repo | Connect GitHub app in dashboard Integrations |
| Save button greyed out | Add trigger + instructions + enable "Comment on PR" tool |
| Agent runs but no PR comment | GitHub App needs **Pull requests: Read & write** on the repo |
| Prefill from agent was empty | Normal — use manual steps above |

## Files

| File | Purpose |
|------|---------|
| `automation-essential.json` | Reference JSON (not auto-imported) |
| `automation-essential-instructions.txt` | Paste into Instructions |
| `automation-trading-bot-absence.json` | Optional PR + weekly/monthly cron |
