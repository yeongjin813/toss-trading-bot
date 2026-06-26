#!/usr/bin/env bash
# Install EC2 healthcheck cron (every 15 min). Alerts via Telegram on failure only.
# Usage: bash deploy/install-healthcheck-cron.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOT_DIR="${BOT_DIR:-${REPO_ROOT}}"
PYTHON="${BOT_DIR}/.venv/bin/python"
CRON_TAG="toss-bot-healthcheck"
CRON_LINE="*/15 * * * * cd ${BOT_DIR} && ${PYTHON} scripts/ec2_healthcheck.py >> /tmp/toss_healthcheck.log 2>&1 # ${CRON_TAG}"

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

echo "Dry-run:"
cd "${BOT_DIR}"
"${PYTHON}" scripts/ec2_healthcheck.py --dry-run || true
