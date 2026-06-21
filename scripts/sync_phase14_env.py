"""Merge Phase 14 non-secret keys from .env.example into .env (EC2 deploy helper)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"

KEYS_TO_SYNC = [
    "WATCHLIST",
    "USE_REGIME_GOLDEN_CROSS",
    "REGIME_CAUTIOUS_MAX_POSITIONS",
    "USE_VOL_ADJUSTED_RISK",
    "VOL_TARGET_PCT",
    "USE_SCALE_IN",
    "USE_SCALE_OUT",
    "USE_WEEKLY_TREND_FILTER",
    "WEEKLY_TREND_SMA_PERIOD",
    "USE_52W_HIGH_FILTER",
    "NEAR_52W_HIGH_PCT",
    "MAX_POSITIONS_PER_SECTOR",
    "MOMENTUM_SECTOR_DIVERSIFY",
    "MOMENTUM_MAX_PER_SECTOR",
    "MOMENTUM_REQUIRE_NEAR_52W_HIGH",
    "MOMENTUM_NEAR_52W_HIGH_PCT",
    "PENDING_ORDER_CANCEL_MINUTES",
    "MAX_CONSECUTIVE_LOSS_DAYS",
    "USE_SPY_MARKET_FILTER",
    "ENTRY_CONFIRMATION_DAYS",
    "USE_VIX_REGIME_FILTER",
    "VIX_REGIME_MAX",
    "SLIPPAGE_BPS",
    "BACKTEST_FILL_AT_NEXT_OPEN",
    "USE_WEEKLY_TELEGRAM_REPORT",
    "LEGACY_CAPITAL_PCT",
    "TOP3_CAPITAL_PCT",
]


def main() -> int:
    if not ENV_PATH.exists():
        raise SystemExit(f"Missing {ENV_PATH}")

    example: dict[str, str] = {}
    for line in EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        example[key] = value

    env_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    present: set[str] = set()
    out: list[str] = []

    for line in env_lines:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in KEYS_TO_SYNC and key in example:
                out.append(f"{key}={example[key]}")
                present.add(key)
            else:
                out.append(line)
                if key in KEYS_TO_SYNC:
                    present.add(key)
        else:
            out.append(line)

    for key in KEYS_TO_SYNC:
        if key not in present and key in example:
            out.append(f"{key}={example[key]}")

    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"Synced {len(KEYS_TO_SYNC)} non-secret keys into {ENV_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
