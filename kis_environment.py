"""
KIS API environment resolution and live-account safety guards.

Production default is VTS sandbox. Switching to a real account requires explicit
``KIS_ENVIRONMENT=live`` plus ``KIS_LIVE_CONFIRMED=true`` (and no accidental URL override).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

KIS_VTS_BASE_URL = "https://openapivts.koreainvestment.com:29443"
KIS_LIVE_BASE_URL = "https://openapi.koreainvestment.com:9443"

_CONFIRM_FLAGS = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class KISEnvironment:
    base_url: str
    is_vts: bool
    environment: str
    tr_id_us_buy: str
    tr_id_us_sell: str
    tr_id_us_ccnl: str
    tr_id_us_nccs: str
    tr_id_us_cancel: str
    default_order_type: str

    def banner_label(self) -> str:
        if self.is_vts:
            return "KIS VTS (mock sandbox)"
        return "*** KIS LIVE (real money) ***"


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _CONFIRM_FLAGS


def load_kis_environment() -> KISEnvironment:
    """Resolve base URL and TR IDs from env; fail fast on label/URL mismatch."""
    explicit = os.getenv("KIS_BASE_URL", "").strip().rstrip("/")
    env_label = os.getenv("KIS_ENVIRONMENT", "vts").strip().lower()

    if env_label not in {"vts", "live"}:
        env_label = "vts"

    if explicit:
        base_url = explicit
    elif env_label == "live":
        base_url = KIS_LIVE_BASE_URL
    else:
        base_url = KIS_VTS_BASE_URL
        env_label = "vts"

    is_vts = "openapivts" in base_url.lower()

    if env_label == "live" and is_vts:
        raise RuntimeError(
            "KIS_ENVIRONMENT=live but URL is VTS sandbox. "
            "Set KIS_BASE_URL to the live API endpoint or use KIS_ENVIRONMENT=vts."
        )
    if env_label == "vts" and not is_vts:
        raise RuntimeError(
            "KIS_ENVIRONMENT=vts but KIS_BASE_URL points to a live API host. "
            "Remove KIS_BASE_URL override or set KIS_ENVIRONMENT=live with "
            "KIS_LIVE_CONFIRMED=true."
        )

    prefix = "VTT" if is_vts else "TTT"
    order_default = "limit" if is_vts else "market"

    explicit_order = os.getenv("KIS_ORDER_TYPE", "").strip().lower()
    if explicit_order in {"limit", "market"}:
        order_default = explicit_order

    return KISEnvironment(
        base_url=base_url,
        is_vts=is_vts,
        environment="vts" if is_vts else "live",
        tr_id_us_buy=f"{prefix}T1002U",
        tr_id_us_sell=f"{prefix}T1006U",
        tr_id_us_ccnl=f"{prefix}S3035R",
        tr_id_us_nccs=f"{prefix}S3018R",
        tr_id_us_cancel=f"{prefix}T1004U",
        default_order_type=order_default,
    )


def validate_kis_live_guard(
    env: KISEnvironment,
    *,
    dry_run: bool,
    capital_at_risk: float,
) -> None:
    """Block accidental live trading without explicit confirmation."""
    if env.is_vts:
        return

    if dry_run:
        logger.warning(
            "KIS base URL is LIVE but KIS_DRY_RUN=true — orders will not hit the broker."
        )
        return

    if not _env_flag("KIS_LIVE_CONFIRMED"):
        raise RuntimeError(
            "Live KIS API detected. Set KIS_LIVE_CONFIRMED=true in .env only after "
            "reviewing account, capital limits, and order routing. "
            "Use KIS_ENVIRONMENT=vts for sandbox."
        )

    max_live = float(os.getenv("KIS_LIVE_MAX_CAPITAL", "500000"))
    if capital_at_risk > max_live and not _env_flag("KIS_LIVE_HIGH_CAPITAL_CONFIRMED"):
        raise RuntimeError(
            f"CAPITAL_AT_RISK=${capital_at_risk:,.0f} exceeds KIS_LIVE_MAX_CAPITAL="
            f"${max_live:,.0f}. Set KIS_LIVE_HIGH_CAPITAL_CONFIRMED=true if intentional."
        )

    logger.critical(
        "LIVE KIS ACCOUNT ACTIVE — real orders and real money. base_url=%s capital=$%s",
        env.base_url,
        f"{capital_at_risk:,.0f}",
    )
