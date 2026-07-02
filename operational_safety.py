"""
Operational kill switches and live-account guardrails for unattended deployment.

Does not change strategy parameters — only gates order placement and startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from execution_engine import ExecutionSettings
    from kis_environment import KISEnvironment

_CONFIRM_FLAGS = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in _CONFIRM_FLAGS


@dataclass(frozen=True)
class KillSwitchSettings:
    trading_paused: bool
    allow_new_buys: bool
    emergency_liquidate: bool

    @classmethod
    def from_env(cls) -> KillSwitchSettings:
        raw_buys = os.getenv("ALLOW_NEW_BUYS", "true").strip().lower()
        allow_buys = raw_buys not in {"false", "0", "no", "off"}
        return cls(
            trading_paused=_env_flag("TRADING_PAUSED"),
            allow_new_buys=allow_buys,
            emergency_liquidate=_env_flag("EMERGENCY_LIQUIDATE"),
        )


def kill_switch_settings_from_env() -> KillSwitchSettings:
    return KillSwitchSettings.from_env()


def describe_active_switches(settings: KillSwitchSettings | None = None) -> list[str]:
    settings = settings or kill_switch_settings_from_env()
    active: list[str] = []
    if settings.trading_paused:
        active.append("TRADING_PAUSED")
    if not settings.allow_new_buys:
        active.append("ALLOW_NEW_BUYS=false")
    if settings.emergency_liquidate:
        active.append("EMERGENCY_LIQUIDATE")
    return active


def check_order_placement_allowed(
    side: str,
    *,
    settings: KillSwitchSettings | None = None,
    emergency_liquidation_sell: bool = False,
) -> str | None:
    """
    Return a block reason when broker order placement must not proceed.

    emergency_liquidation_sell: SELL issued by the emergency liquidation pass
    (allowed even when TRADING_PAUSED).
    """
    settings = settings or kill_switch_settings_from_env()
    normalized = side.upper()

    if emergency_liquidation_sell and normalized == "SELL":
        return None

    if settings.emergency_liquidate:
        if normalized == "SELL":
            return None
        return "EMERGENCY_LIQUIDATE=true — new BUY orders blocked"

    if settings.trading_paused:
        return "TRADING_PAUSED=true — all broker orders blocked (health/reconcile/logging still run)"

    if normalized == "BUY" and not settings.allow_new_buys:
        return "ALLOW_NEW_BUYS=false — new BUY orders blocked (risk exits still allowed)"

    return None


def _telegram_configured() -> bool:
    enabled = _env_flag("USE_TELEGRAM_ALERTS", "true")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return enabled and bool(token) and bool(chat_id)


def validate_live_account_requirements(
    env: KISEnvironment,
    *,
    dry_run: bool,
    capital_at_risk: float,
    execution_settings: ExecutionSettings,
) -> None:
    """Extra live guardrails beyond kis_environment.validate_kis_live_guard."""
    if env.is_vts or dry_run:
        return

    missing: list[str] = []

    if not _env_flag("KIS_LIVE_CONFIRMED"):
        missing.append("KIS_LIVE_CONFIRMED=true")

    if not _telegram_configured():
        missing.append(
            "USE_TELEGRAM_ALERTS=true with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
        )

    if capital_at_risk <= 0:
        missing.append("CAPITAL_AT_RISK > 0")

    if execution_settings.max_portfolio_usd <= 0:
        missing.append("MAX_PORTFOLIO_USD > 0")

    if execution_settings.max_ticker_exposure_usd <= 0:
        missing.append("MAX_TICKER_EXPOSURE_USD > 0")

    if execution_settings.max_daily_loss_usd <= 0:
        missing.append("MAX_DAILY_LOSS_USD > 0")

    if execution_settings.max_open_positions <= 0:
        missing.append("MAX_OPEN_POSITIONS > 0")

    if missing:
        raise RuntimeError(
            "Live trading blocked — fix the following before starting: "
            + "; ".join(missing)
        )


def collect_open_holdings(
    states: dict[str, Any],
    watchlist: list[str],
) -> list[tuple[str, int]]:
    portfolio = states.get("_portfolio", {})
    broker = portfolio.get("broker_holdings") or {}
    rows: list[tuple[str, int]] = []
    seen: set[str] = set()
    for ticker in watchlist:
        key = ticker.upper()
        qty = int(broker.get(ticker, broker.get(key, 0)) or 0)
        if qty <= 0:
            payload = states.get(ticker, {})
            if isinstance(payload, dict):
                qty = int(payload.get("held_quantity", 0) or 0)
        if qty > 0 and key not in seen:
            rows.append((ticker.upper(), qty))
            seen.add(key)
    return rows
