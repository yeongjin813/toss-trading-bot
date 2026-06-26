"""
EC2 ops healthcheck — Telegram alert when toss-bot or disk is unhealthy.

Designed for unattended prod (cron every 15 min). Does not change strategy or .env.

Usage:
  .venv/bin/python scripts/ec2_healthcheck.py
  .venv/bin/python scripts/ec2_healthcheck.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from telegram_notifier import TelegramConfig, run_telegram_sync, send_system_alert

DEFAULT_SERVICE = "toss-bot"
DEFAULT_DISK_WARN_PCT = 80
DEFAULT_LOG_WARN_MB = 400
DEFAULT_COOLDOWN_SEC = 14_400  # 4 hours between repeat alerts for same issue


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _service_active(service: str) -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            check=False,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return True


def _disk_used_pct(path: str = "/") -> float:
    usage = shutil.disk_usage(path)
    if usage.total <= 0:
        return 0.0
    return (usage.used / usage.total) * 100.0


def _log_size_mb(log_path: Path) -> float:
    if not log_path.is_file():
        return 0.0
    return log_path.stat().st_size / (1024 * 1024)


def _load_state(path: Path) -> dict[str, float]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def _save_state(path: Path, state: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _should_alert(state: dict[str, float], key: str, cooldown_sec: int) -> bool:
    last = state.get(key, 0.0)
    return (time.time() - last) >= cooldown_sec


def run_checks(
    *,
    bot_dir: Path,
    service: str,
    disk_warn_pct: float,
    log_warn_mb: float,
) -> list[tuple[str, str]]:
    """Return list of (level, message) for active issues."""
    issues: list[tuple[str, str]] = []

    if not _service_active(service):
        issues.append(("CRITICAL", f"{service} is not active (systemctl is-active failed)"))

    used_pct = _disk_used_pct("/")
    if used_pct >= disk_warn_pct:
        issues.append(
            (
                "WARNING",
                f"Root disk {used_pct:.1f}% used (threshold {disk_warn_pct:.0f}%)",
            )
        )

    metrics_log = bot_dir / "project_metrics.log"
    log_mb = _log_size_mb(metrics_log)
    if log_mb >= log_warn_mb:
        issues.append(
            (
                "WARNING",
                f"project_metrics.log is {log_mb:.0f} MB (threshold {log_warn_mb:.0f} MB) — check logrotate",
            )
        )

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="EC2 Toss bot healthcheck")
    parser.add_argument("--dry-run", action="store_true", help="Print issues only; no Telegram")
    args = parser.parse_args()

    bot_dir = Path(os.getenv("BOT_DIR", str(ROOT)))
    service = os.getenv("HEALTHCHECK_SERVICE", DEFAULT_SERVICE).strip() or DEFAULT_SERVICE
    disk_warn = _env_float("HEALTHCHECK_DISK_WARN_PCT", DEFAULT_DISK_WARN_PCT)
    log_warn = _env_float("HEALTHCHECK_LOG_WARN_MB", DEFAULT_LOG_WARN_MB)
    cooldown = _env_int("HEALTHCHECK_ALERT_COOLDOWN_SEC", DEFAULT_COOLDOWN_SEC)
    state_path = Path(
        os.getenv(
            "HEALTHCHECK_STATE_FILE",
            f"/tmp/toss_bot_healthcheck_{service}.json",
        )
    )

    issues = run_checks(
        bot_dir=bot_dir,
        service=service,
        disk_warn_pct=disk_warn,
        log_warn_mb=log_warn,
    )

    if not issues:
        print(f"OK: {service} active, disk and logs within thresholds")
        return 0

    for level, message in issues:
        print(f"[{level}] {message}")

    if args.dry_run:
        return 1

    config = TelegramConfig.from_env()
    if not config.enabled or not config.bot_token or not config.chat_id:
        print("Telegram disabled or not configured — alerts not sent")
        return 1

    state = _load_state(state_path)
    sent = 0
    for level, message in issues:
        key = f"{level}:{message}"
        if not _should_alert(state, key, cooldown):
            print(f"Throttled: {message}")
            continue
        run_telegram_sync(send_system_alert(level, f"[HEALTHCHECK] {message}", config=config))
        state[key] = time.time()
        sent += 1

    if sent:
        _save_state(state_path, state)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
