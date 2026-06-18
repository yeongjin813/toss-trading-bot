"""
Four-phase dual-strategy deployment controls.

Phase 1 — legacy signal engine only (no Top3 live overlay).
Phase 2 — Top3 backtests only (TOP3_BACKTEST_ONLY; live unchanged).
Phase 3 — legacy live + Top3 shadow dry-run (log/Telegram, no KIS for Top3).
Phase 4 — live capital split (LEGACY_CAPITAL_PCT / TOP3_CAPITAL_PCT).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DeploymentConfig:
    phase: int
    strategy_mode: str
    top3_backtest_only: bool
    top3_dry_run_enabled: bool
    legacy_capital_pct: float
    top3_capital_pct: float

    @classmethod
    def from_env(cls) -> DeploymentConfig:
        phase = max(1, min(4, int(os.getenv("DEPLOYMENT_PHASE", "1"))))
        mode = os.getenv("STRATEGY_MODE", "legacy").strip().lower()
        if mode not in {"legacy", "dual"}:
            mode = "legacy"
        legacy_pct = float(os.getenv("LEGACY_CAPITAL_PCT", "60"))
        top3_pct = float(os.getenv("TOP3_CAPITAL_PCT", "40"))
        return cls(
            phase=phase,
            strategy_mode=mode,
            top3_backtest_only=_flag("TOP3_BACKTEST_ONLY", "false"),
            top3_dry_run_enabled=_flag("TOP3_DRY_RUN_ENABLED", "false"),
            legacy_capital_pct=legacy_pct,
            top3_capital_pct=top3_pct,
        )

    @property
    def is_dual(self) -> bool:
        return self.strategy_mode == "dual" and self.phase >= 3

    @property
    def top3_shadow_active(self) -> bool:
        """Phase 3+: simulate Top3 orders (no KIS when dry-run only)."""
        return self.is_dual and (
            self.phase == 3 and self.top3_dry_run_enabled or self.phase >= 4
        )

    @property
    def top3_live_orders(self) -> bool:
        """Phase 4: Top3 may place real broker orders (still respects KIS_DRY_RUN)."""
        return self.is_dual and self.phase >= 4

    def legacy_capital_fraction(self) -> float:
        if not self.is_dual:
            return 1.0
        total = self.legacy_capital_pct + self.top3_capital_pct
        if total <= 0:
            return 1.0
        return self.legacy_capital_pct / total

    def top3_capital_fraction(self) -> float:
        if not self.is_dual:
            return 0.0
        total = self.legacy_capital_pct + self.top3_capital_pct
        if total <= 0:
            return 0.0
        return self.top3_capital_pct / total

    def legacy_momentum_rank_enabled(self, env_enabled: bool) -> bool:
        """Dual mode runs Top3 separately; legacy path skips momentum Top-N gate."""
        if self.is_dual:
            return False
        return env_enabled

    def describe(self) -> str:
        parts = [f"phase={self.phase}", f"mode={self.strategy_mode}"]
        if self.is_dual:
            parts.append(
                f"capital={self.legacy_capital_pct:.0f}/{self.top3_capital_pct:.0f}"
            )
        if self.top3_shadow_active:
            parts.append(
                "top3=shadow" if self.phase == 3 else "top3=live-split"
            )
        if self.top3_backtest_only:
            parts.append("backtest-only")
        return ", ".join(parts)


def scaled_capital(base_capital: float, fraction: float) -> float:
    return max(0.0, base_capital * fraction)
