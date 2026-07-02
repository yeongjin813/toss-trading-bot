"""
Daily backup of runtime artifacts for long unattended operation.

Does NOT copy .env or secrets. Safe to run from cron once per day.

Usage:
  python scripts/daily_backup.py
  python scripts/daily_backup.py --dest ./backups
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SAFE_ENV_KEYS = (
    "KIS_ENVIRONMENT",
    "DEPLOYMENT_PHASE",
    "STRATEGY_MODE",
    "CAPITAL_AT_RISK",
    "LEGACY_CAPITAL_PCT",
    "TOP3_CAPITAL_PCT",
    "MAX_OPEN_POSITIONS",
    "MAX_PORTFOLIO_USD",
    "MAX_TICKER_EXPOSURE_USD",
    "MAX_DAILY_LOSS_USD",
    "TRADING_PAUSED",
    "ALLOW_NEW_BUYS",
    "EMERGENCY_LIQUIDATE",
    "USE_TELEGRAM_ALERTS",
    "KIS_DRY_RUN",
    "MOMENTUM_TOP_N",
    "WATCHLIST",
)


def _redacted_config_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for key in SAFE_ENV_KEYS:
        value = os.getenv(key, "").strip()
        if not value:
            continue
        if key == "WATCHLIST" and len(value) > 120:
            snapshot[key] = value[:120] + "..."
        else:
            snapshot[key] = value
    snapshot["backed_up_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return snapshot


def _copy_file(src: Path, dest: Path) -> bool:
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def _copy_log_maybe_compressed(src: Path, dest: Path, *, compress_mb: float = 5.0) -> bool:
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    size_mb = src.stat().st_size / (1024 * 1024)
    if size_mb >= compress_mb:
        with src.open("rb") as handle_in, gzip.open(f"{dest}.gz", "wb") as handle_out:
            shutil.copyfileobj(handle_in, handle_out)
    else:
        shutil.copy2(src, dest)
    return True


def run_backup(
    *,
    bot_dir: Path,
    dest_root: Path,
    compress_log_mb: float = 5.0,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = dest_root / stamp
    dest.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for name in ("trading_state.json", "trade_log.csv", "heartbeat.json", "order_retry_queue.json"):
        if _copy_file(bot_dir / name, dest / name):
            copied.append(name)

    if _copy_log_maybe_compressed(
        bot_dir / "project_metrics.log",
        dest / "project_metrics.log",
        compress_mb=compress_log_mb,
    ):
        copied.append("project_metrics.log")

    config_path = dest / "config_snapshot.json"
    config_path.write_text(
        json.dumps(_redacted_config_snapshot(), indent=2),
        encoding="utf-8",
    )
    copied.append("config_snapshot.json")

    manifest = {
        "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "bot_dir": str(bot_dir),
        "files": copied,
        "secrets_excluded": [".env", "kis_token_cache.json"],
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup Toss bot runtime files (no secrets)")
    parser.add_argument("--bot-dir", default=str(ROOT), help="Bot project directory")
    parser.add_argument(
        "--dest",
        default=os.getenv("BACKUP_DIR", str(ROOT / "backups")),
        help="Backup root directory",
    )
    parser.add_argument(
        "--compress-log-mb",
        type=float,
        default=float(os.getenv("BACKUP_COMPRESS_LOG_MB", "5")),
        help="Gzip project_metrics.log when larger than this many MB",
    )
    args = parser.parse_args()

    dest = run_backup(
        bot_dir=Path(args.bot_dir),
        dest_root=Path(args.dest),
        compress_log_mb=args.compress_log_mb,
    )
    print(f"Backup written to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
