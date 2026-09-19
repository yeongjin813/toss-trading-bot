#!/usr/bin/env bash
# Install daily backup cron (01:15 UTC). No secrets copied.
# Usage: bash deploy/install-daily-backup-cron.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOT_DIR="${BOT_DIR:-${REPO_ROOT}}"
PYTHON="${BOT_DIR}/.venv/bin/python"
CRON_TAG="toss-bot-daily-backup"
CRON_LINE="15 1 * * * cd ${BOT_DIR} && ${PYTHON} scripts/daily_backup.py >> /tmp/toss_daily_backup.log 2>&1 # ${CRON_TAG}"

if [[ ! -x "${PYTHON}" ]]; then
  echo "Missing venv python at ${PYTHON}" >&2
  exit 1
fi

existing="$(crontab -l 2>/dev/null || true)"
if echo "${existing}" | grep -q "${CRON_TAG}"; then
  echo "Cron already installed (${CRON_TAG})"
else
  (echo "${existing}"; echo "${CRON_LINE}") | crontab -
  echo "Installed crontab entry:"
  echo "  ${CRON_LINE}"
fi

echo "Smoke backup:"
cd "${BOT_DIR}"
"${PYTHON}" scripts/daily_backup.py || true
