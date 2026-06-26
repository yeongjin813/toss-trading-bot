#!/usr/bin/env bash
# Install logrotate policy for Toss Trading Bot on EC2.
# Usage (on server): bash deploy/install-logrotate.sh
# Optional: BOT_DIR=/path/to/repo bash deploy/install-logrotate.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOT_DIR="${BOT_DIR:-${REPO_ROOT}}"
CONF_SRC="${SCRIPT_DIR}/logrotate-toss-bot.conf"
CONF_DST="/etc/logrotate.d/toss-bot"

if [[ ! -f "${CONF_SRC}" ]]; then
  echo "Missing ${CONF_SRC}" >&2
  exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
  SUDO="sudo"
else
  SUDO=""
fi

tmp="$(mktemp)"
sed "s|/home/ubuntu/toss-trading-bot|${BOT_DIR}|g" "${CONF_SRC}" > "${tmp}"
${SUDO} install -m 0644 "${tmp}" "${CONF_DST}"
rm -f "${tmp}"

echo "Installed ${CONF_DST} (BOT_DIR=${BOT_DIR})"
${SUDO} logrotate -d "${CONF_DST}" 2>&1 | head -20
echo "Dry-run complete. logrotate runs daily via /etc/cron.daily/logrotate."
