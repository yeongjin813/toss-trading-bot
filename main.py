"""
Production execution loop — Korea Investment & Securities (KIS) mock trading API.

Setup:
  Create a `.env` file in the project root with the following keys:

    KIS_APP_KEY=your_app_key
    KIS_APP_SECRET=your_app_secret
    KIS_CANO=your_account_number
    KIS_ACNT_PRDT_CD=01
    WATCHLIST=AAPL,MSFT,NVDA,META,AMZN,GOOGL,TSLA,AMD,AVGO,NFLX,PLTR,CRWD,TSM,SHOP,UBER
    USE_SPY_MARKET_FILTER=true
    CAPITAL_AT_RISK=100000
    KIS_ORDER_TYPE=limit
    KIS_LIMIT_PRICE_BUFFER_BPS=10

Run:
  python main.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

from analytics import (
    LiveSignalEngine,
    PositionState,
    describe_us_market_closure,
    is_us_equity_session,
    is_us_regular_market_hours,
    market_regime_snapshot,
    seconds_until_us_rth_open,
    spy_regime_snapshot,
    use_eod_atr_stops,
)
from config import StrategyConfigMapper
from market_registry import (
    BENCHMARK_SMA_PERIOD,
    BENCHMARK_TICKER,
    MARKET_META,
    SECONDARY_BENCHMARK_TICKER,
    parse_watchlist,
    validate_watchlist_routing,
)
from strategy import DEFAULT_COMMISSION_RATE
from execution_engine import (
    ExecutionSettings,
    OrderFillMonitor,
    RiskGuard,
    TradeLogWriter,
    assign_open_order,
    block_new_buy_rth_window,
    extract_order_odno,
    limit_buffer_bps_for_ticker,
    limit_order_price,
)
from session_manager import (
    LiveExecutionGatekeeper,
    PortfolioLedger,
    PortfolioReconciliationEngine,
    RegularHoursGate,
)
from telegram_notifier import (
    TelegramConfig,
    send_eod_report,
    send_system_alert,
    send_trade_report,
)
from kis_http import is_retryable_request_error, kis_request, last_api_response_ms
from order_retry_queue import OrderRetryQueue, PendingOrderRetry
from market_data_cache import MarketDataCache
from momentum_ranker import (
    MomentumRankSettings,
    build_cycle_tickers,
    is_new_buy_allowed,
    rebalance_active_tickers,
)
from daily_report import (
    compile_eod_metrics,
    format_eod_report_text,
    is_dry_run_mode,
    mark_eod_report_sent,
    should_send_eod_report,
    use_daily_telegram_report,
)
from deployment_config import DeploymentConfig, scaled_capital
from top3_strategy import (
    compute_top3_rebalance_orders,
    load_top3_state,
    save_top3_state,
)

load_dotenv(override=True)

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
CANO = os.getenv("KIS_CANO")
ACNT_PRDT_CD = os.getenv("KIS_ACNT_PRDT_CD")

BASE_URL = "https://openapivts.koreainvestment.com:29443"

TR_ID_US_BUY = "VTTT1002U"
TR_ID_US_SELL = "VTTT1006U"
TR_ID_DAILY_PRICE = "HHDFS76240000"
TR_ID_US_PRESENT_BALANCE = "VTRP6504R"
TR_ID_US_CCNL = "VTTS3035R" if "openapivts" in BASE_URL else "TTTS3035R"
TR_ID_US_NCCS = "VTTS3018R" if "openapivts" in BASE_URL else "TTTS3018R"
TR_ID_US_CANCEL = "VTTT1004U" if "openapivts" in BASE_URL else "TTTT1004U"

TOKEN_PATH = "/oauth2/tokenP"
HASHKEY_PATH = "/uapi/hashkey"
DAILY_PRICE_PATH = "/uapi/overseas-price/v1/quotations/dailyprice"
ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
PRESENT_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-present-balance"
INQUIRE_CCNL_PATH = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
INQUIRE_NCCS_PATH = "/uapi/overseas-stock/v1/trading/inquire-nccs"
ORDER_RVSECNCL_PATH = "/uapi/overseas-stock/v1/trading/order-rvsecncl"

WATCHLIST = parse_watchlist(os.getenv("WATCHLIST"))
USE_SPY_MARKET_FILTER = StrategyConfigMapper.use_spy_market_filter()
USE_QQQ_REGIME_FILTER = StrategyConfigMapper.use_qqq_regime_filter()
DEPLOYMENT = DeploymentConfig.from_env()
_MOMENTUM_RAW = MomentumRankSettings.from_env()
MOMENTUM_SETTINGS = MomentumRankSettings(
    enabled=DEPLOYMENT.legacy_momentum_rank_enabled(_MOMENTUM_RAW.enabled),
    top_n=_MOMENTUM_RAW.top_n,
    rebalance_weekday=_MOMENTUM_RAW.rebalance_weekday,
    weight_3m=_MOMENTUM_RAW.weight_3m,
    weight_6m=_MOMENTUM_RAW.weight_6m,
    weight_12m=_MOMENTUM_RAW.weight_12m,
    weight_volume=_MOMENTUM_RAW.weight_volume,
    require_above_sma50=_MOMENTUM_RAW.require_above_sma50,
    require_above_sma200=_MOMENTUM_RAW.require_above_sma200,
    min_bars=_MOMENTUM_RAW.min_bars,
)

ANALYTICS_SMA_PERIOD = 20
MIN_DATA_BARS = 22

DATA_DIR = "./data"
STATE_FILE = "./trading_state.json"
TRADE_LOG_FILE = os.getenv("TRADE_LOG_FILE", "./trade_log.csv")
TOKEN_CACHE_FILE = "./kis_token_cache.json"
LOOKBACK_YEARS = 3
TARGET_BARS = LOOKBACK_YEARS * 252
CAPITAL_AT_RISK = float(os.getenv("CAPITAL_AT_RISK", "10000"))
LEGACY_CAPITAL_AT_RISK = scaled_capital(
    CAPITAL_AT_RISK, DEPLOYMENT.legacy_capital_fraction()
)
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
TICKER_SLEEP_SECONDS = int(os.getenv("TICKER_SLEEP_SECONDS", "1"))
LOOP_COOLDOWN_SECONDS = int(os.getenv("LOOP_COOLDOWN_SECONDS", "60"))
MARKET_CLOSED_SLEEP_SECONDS = int(os.getenv("MARKET_CLOSED_SLEEP_SECONDS", "3600"))
KIS_REQUEST_TIMEOUT_SECONDS = int(os.getenv("KIS_REQUEST_TIMEOUT_SECONDS", "30"))
KIS_ORDER_MAX_RETRIES = max(1, int(os.getenv("KIS_ORDER_MAX_RETRIES", "3")))
KIS_ORDER_RETRY_BACKOFF_SECONDS = float(os.getenv("KIS_ORDER_RETRY_BACKOFF_SECONDS", "2.0"))
KIS_SLOW_API_MS = int(os.getenv("KIS_SLOW_API_MS", "3000"))
TELEGRAM_ALERT_THROTTLE_SECONDS = int(os.getenv("TELEGRAM_ALERT_THROTTLE_SECONDS", "900"))
_alert_last_sent: dict[str, float] = {}


def _resolve_kis_order_type() -> str:
    explicit = os.getenv("KIS_ORDER_TYPE", "").strip().lower()
    if explicit in {"limit", "market"}:
        return explicit
    if "openapivts" in BASE_URL:
        return "limit"
    return "market"


KIS_ORDER_TYPE = _resolve_kis_order_type()

EXECUTION_SETTINGS = ExecutionSettings.from_env()
TRADE_LOG = TradeLogWriter(TRADE_LOG_FILE)
RISK_GUARD = RiskGuard(EXECUTION_SETTINGS)
ORDER_RETRY_QUEUE = OrderRetryQueue()

logger = logging.getLogger(__name__)


def _telegram_enabled() -> bool:
    return TelegramConfig.from_env().enabled


def _run_telegram(coro) -> None:
    """Run an async Telegram coroutine from sync code; no-op when alerts disabled."""
    if not _telegram_enabled():
        return

    try:
        asyncio.run(coro)
    except Exception as exc:
        logger.error("Telegram dispatch failed: %s", exc)


def _dispatch_trade_report(
    ticker: str,
    action: str,
    quantity: int,
    price: float,
    execution_time: datetime,
    *,
    fill_status: str = "FILLED",
) -> None:
    logger.info(
        "[TRADE/%s] %s %s qty=%s @ $%.2f",
        fill_status,
        action,
        ticker,
        quantity,
        price,
    )
    if not _telegram_enabled():
        logger.debug("USE_TELEGRAM_ALERTS=false — trade report logged locally only")
        return

    _run_telegram(
        send_trade_report(
            ticker,
            action,
            quantity,
            price,
            execution_time,
        )
    )
    logger.info("Telegram trade report sent for %s %s", action, ticker)


def _is_transient_kis_failure(exc: BaseException) -> bool:
    if is_retryable_request_error(exc):
        return True
    text = str(exc)
    return "500 Server Error" in text or "502 " in text or "503 " in text


def _dispatch_system_alert(
    level: str,
    message: str,
    *,
    telegram: bool | None = None,
) -> None:
    level_upper = level.upper()
    log_fn = {
        "CRITICAL": logger.critical,
        "WARNING": logger.warning,
        "INFO": logger.info,
    }.get(level_upper, logger.info)
    log_fn("[ALERT/%s] %s", level_upper, message)

    if not _telegram_enabled():
        logger.debug("USE_TELEGRAM_ALERTS=false — system alert logged locally only")
        return

    send_telegram = telegram
    if send_telegram is None:
        send_telegram = level_upper != "WARNING" or is_us_regular_market_hours()
    if not send_telegram:
        logger.debug("Outside RTH — %s alert logged locally only (no Telegram)", level_upper)
        return

    if level_upper == "CRITICAL" and TELEGRAM_ALERT_THROTTLE_SECONDS > 0:
        throttle_key = message[:120]
        now = time.time()
        last = _alert_last_sent.get(throttle_key, 0.0)
        if now - last < TELEGRAM_ALERT_THROTTLE_SECONDS:
            logger.warning(
                "Throttled duplicate CRITICAL Telegram alert (%ds): %s",
                TELEGRAM_ALERT_THROTTLE_SECONDS,
                throttle_key[:80],
            )
            return
        _alert_last_sent[throttle_key] = now

    _run_telegram(send_system_alert(level_upper, message))


def _handle_trade_fill(
    ticker: str,
    side: str,
    quantity: int,
    fill_price: float,
    status: str,
) -> None:
    action = "SELL" if side in {"SELL", "DYNAMIC_ATR_SELL"} else "BUY"
    _dispatch_trade_report(
        ticker,
        action,
        quantity,
        fill_price,
        datetime.now(),
        fill_status=status,
    )


def _is_auth_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    if any(
        keyword in text
        for keyword in (
            "401",
            "403",
            "unauthorized",
            "authentication",
            "token issuance",
            "access_token",
        )
    ):
        return True
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in {401, 403}


FILL_MONITOR = OrderFillMonitor(
    EXECUTION_SETTINGS,
    TRADE_LOG,
    on_fill=_handle_trade_fill,
    on_alert=_dispatch_system_alert,
)

_reconciliation_engine = PortfolioReconciliationEngine(WATCHLIST)
_execution_gatekeeper = LiveExecutionGatekeeper()
_rth_gate = RegularHoursGate()


def validate_environment() -> None:
    """Ensure all required KIS credentials are present in the environment."""
    missing = [
        name
        for name, value in {
            "KIS_APP_KEY": APP_KEY,
            "KIS_APP_SECRET": APP_SECRET,
            "KIS_CANO": CANO,
            "KIS_ACNT_PRDT_CD": ACNT_PRDT_CD,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Create a .env file with these keys before running."
        )

    unknown = validate_watchlist_routing(WATCHLIST)
    if unknown:
        raise RuntimeError(
            f"Watchlist tickers missing MARKET_META routing: {', '.join(unknown)}"
        )
    if USE_SPY_MARKET_FILTER and BENCHMARK_TICKER not in MARKET_META:
        raise RuntimeError(
            f"SPY market filter enabled but {BENCHMARK_TICKER} is missing MARKET_META routing."
        )
    if USE_QQQ_REGIME_FILTER and SECONDARY_BENCHMARK_TICKER not in MARKET_META:
        raise RuntimeError(
            f"QQQ regime filter enabled but {SECONDARY_BENCHMARK_TICKER} is missing MARKET_META routing."
        )


class KISApiClient:
    """Minimal KIS Open API client for US mock trading."""

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None

    def _load_cached_token(self) -> bool:
        if not os.path.exists(TOKEN_CACHE_FILE):
            return False

        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        expires_at = datetime.fromisoformat(payload["expires_at"])
        if datetime.now() >= expires_at:
            return False

        self._access_token = payload["access_token"]
        self._token_expires_at = expires_at
        return True

    def _save_cached_token(self) -> None:
        if not self._access_token or not self._token_expires_at:
            return

        with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "access_token": self._access_token,
                    "expires_at": self._token_expires_at.isoformat(),
                },
                handle,
                indent=2,
            )

    def get_access_token(self) -> str:
        if self._access_token and self._token_expires_at and datetime.now() < self._token_expires_at:
            return self._access_token

        if self._load_cached_token():
            return self._access_token  # type: ignore[return-value]

        url = f"{BASE_URL}{TOKEN_PATH}"
        body = {
            "grant_type": "client_credentials",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
        }
        response = kis_request(
            "POST",
            url,
            label="token",
            json=body,
            timeout=KIS_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("access_token") is None:
            raise RuntimeError(f"Token issuance failed: {payload}")

        self._access_token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 86400))
        self._token_expires_at = datetime.now() + timedelta(seconds=expires_in - 60)
        self._save_cached_token()
        return self._access_token

    def _build_headers(
        self,
        tr_id: str,
        include_hashkey: bool = False,
        hashkey: str = "",
    ) -> dict[str, str]:
        token = self.get_access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": APP_KEY,  # type: ignore[dict-item]
            "appsecret": APP_SECRET,  # type: ignore[dict-item]
            "tr_id": tr_id,
            "custtype": "P",
        }
        if include_hashkey:
            headers["hashkey"] = hashkey
        return headers

    def _kis_get_with_retry(
        self,
        *,
        path: str,
        tr_id: str,
        params: dict[str, Any],
        label: str,
        error_prefix: str,
        max_retries: int = KIS_ORDER_MAX_RETRIES,
    ) -> dict[str, Any]:
        """GET a KIS endpoint with exponential backoff on transient HTTP failures."""
        url = f"{BASE_URL}{path}"
        last_error: requests.RequestException | None = None

        for attempt in range(max_retries):
            try:
                response = kis_request(
                    "GET",
                    url,
                    label=label,
                    headers=self._build_headers(tr_id),
                    params=params,
                    timeout=KIS_REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("rt_cd") not in (None, "0"):
                    raise RuntimeError(
                        f"{error_prefix}: "
                        f"{payload.get('msg_cd')} | {payload.get('msg1')}"
                    )
                return payload
            except requests.RequestException as exc:
                last_error = exc
                if not is_retryable_request_error(exc) or attempt >= max_retries - 1:
                    raise
                wait_seconds = KIS_ORDER_RETRY_BACKOFF_SECONDS * (2**attempt)
                print(
                    f"[KIS/RETRY] {label} attempt {attempt + 2}/{max_retries} "
                    f"in {wait_seconds:.1f}s — {exc}"
                )
                time.sleep(wait_seconds)

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"{error_prefix}: unknown error")

    def get_hashkey(self, body: dict[str, Any]) -> str:
        url = f"{BASE_URL}{HASHKEY_PATH}"
        token = self.get_access_token()
        response = kis_request(
            "POST",
            url,
            label="hashkey",
            headers={
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": APP_KEY,
                "appsecret": APP_SECRET,
            },
            json=body,
            timeout=KIS_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        hashkey = payload.get("HASH")
        if not hashkey:
            raise RuntimeError(f"Hashkey issuance failed: {payload}")
        return hashkey

    def _fetch_daily_price_batch(
        self,
        ticker: str,
        excd: str,
        bymd: str,
        *,
        max_retries: int = 3,
    ) -> list[dict[str, Any]]:
        url = f"{BASE_URL}{DAILY_PRICE_PATH}"
        params = {
            "AUTH": "",
            "EXCD": excd,
            "SYMB": ticker,
            "GUBN": "0",
            "BYMD": bymd,
            "MODP": "1",
        }

        last_error: requests.RequestException | None = None
        for attempt in range(max_retries):
            try:
                response = kis_request(
                    "GET",
                    url,
                    label=f"dailyprice:{ticker}",
                    headers=self._build_headers(TR_ID_DAILY_PRICE),
                    params=params,
                    timeout=KIS_REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                payload = response.json()

                if payload.get("rt_cd") not in (None, "0"):
                    raise RuntimeError(
                        f"Daily price API error for {ticker}: "
                        f"{payload.get('msg_cd')} | {payload.get('msg1')}"
                    )

                return payload.get("output2") or []
            except requests.RequestException as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status >= 500 and attempt < max_retries - 1:
                    wait_seconds = 1.0 * (attempt + 1)
                    print(
                        f"[WARN] KIS dailyprice {status} for {ticker} "
                        f"(BYMD={bymd}) - retry {attempt + 2}/{max_retries} in {wait_seconds:.0f}s"
                    )
                    time.sleep(wait_seconds)
                    continue
                raise

        if last_error is not None:
            raise last_error
        return []

    def fetch_us_daily_bars(
        self,
        ticker: str,
        excd: str,
        target_bars: int = TARGET_BARS,
    ) -> pd.DataFrame:
        """Fetch US daily OHLCV via TR ID HHDFS76240000 with pagination."""
        collected: list[dict[str, Any]] = []
        bymd = datetime.now().strftime("%Y%m%d")

        while len(collected) < target_bars:
            try:
                batch = self._fetch_daily_price_batch(ticker, excd, bymd)
            except requests.RequestException as exc:
                if len(collected) >= MIN_DATA_BARS:
                    print(
                        f"[WARN] KIS pagination halted for {ticker} with "
                        f"{len(collected)} rows ({exc})"
                    )
                    break
                raise

            if not batch:
                break

            collected.extend(batch)

            oldest = batch[-1].get("xymd") or batch[-1].get("stck_bsop_date")
            if not oldest:
                break

            oldest_dt = datetime.strptime(str(oldest), "%Y%m%d")
            bymd = (oldest_dt - timedelta(days=1)).strftime("%Y%m%d")
            time.sleep(0.25)

            if len(batch) < 50:
                break

        if not collected:
            raise RuntimeError(f"No daily price data returned for {ticker} ({excd}).")

        df = _parse_kis_daily_output(collected)
        df = df.drop_duplicates(subset=["Date"]).sort_values("Date").tail(target_bars)
        return df.reset_index(drop=True)

    def fetch_us_daily_latest(
        self,
        ticker: str,
        excd: str,
    ) -> pd.DataFrame:
        """Fetch only the most recent daily price page (single API call)."""
        bymd = datetime.now().strftime("%Y%m%d")
        batch = self._fetch_daily_price_batch(ticker, excd, bymd)
        if not batch:
            return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
        return _parse_kis_daily_output(batch)

    def place_us_order(
        self,
        side: str,
        ticker: str,
        ovrs_excg_cd: str,
        quantity: int,
        price: float = 0.0,
    ) -> dict[str, Any]:
        """Submit a US equity order with KIS hashkey validation."""
        if quantity <= 0:
            raise ValueError("Order quantity must be greater than zero.")

        tr_id = TR_ID_US_BUY if side.upper() == "BUY" else TR_ID_US_SELL
        order_body = {
            "CANO": CANO,
            "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "PDNO": ticker,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": "0" if price <= 0 else f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "01" if price <= 0 else "00",
        }

        hashkey = self.get_hashkey(order_body)
        url = f"{BASE_URL}{ORDER_PATH}"
        response = kis_request(
            "POST",
            url,
            label=f"order:{side}:{ticker}",
            headers=self._build_headers(tr_id, include_hashkey=True, hashkey=hashkey),
            json=order_body,
            timeout=KIS_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("rt_cd") not in (None, "0"):
            raise RuntimeError(
                f"Order rejected for {ticker}: "
                f"{payload.get('msg_cd')} | {payload.get('msg1')}"
            )

        return payload

    def fetch_overseas_present_balance(
        self,
        natn_cd: str = "840",
        wcrc_frcr_dvsn_cd: str = "02",
        tr_mket_cd: str = "00",
        inqr_dvsn_cd: str = "00",
    ) -> dict[str, Any]:
        """Fetch overseas mock account balance/deposit via inquire-present-balance."""
        params = {
            "CANO": CANO,
            "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "WCRC_FRCR_DVSN_CD": wcrc_frcr_dvsn_cd,
            "NATN_CD": natn_cd,
            "TR_MKET_CD": tr_mket_cd,
            "INQR_DVSN_CD": inqr_dvsn_cd,
        }
        return self._kis_get_with_retry(
            path=PRESENT_BALANCE_PATH,
            tr_id=TR_ID_US_PRESENT_BALANCE,
            params=params,
            label="present-balance",
            error_prefix="Present balance API error",
        )

    def fetch_overseas_order_ccnl(
        self,
        ord_strt_dt: str,
        ord_end_dt: str,
        pdno: str = "",
        ovrs_excg_cd: str = "",
        sll_buy_dvsn: str = "00",
        ccld_nccs_dvsn: str = "00",
    ) -> list[dict[str, Any]]:
        """Fetch overseas order/fill rows for the given NY order-date window."""
        params = {
            "CANO": CANO,
            "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "PDNO": pdno,
            "ORD_STRT_DT": ord_strt_dt,
            "ORD_END_DT": ord_end_dt,
            "SLL_BUY_DVSN": sll_buy_dvsn,
            "CCLD_NCCS_DVSN": ccld_nccs_dvsn,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "SORT_SQN": "DS",
            "ORD_DT": "",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "CTX_AREA_NK200": "",
            "CTX_AREA_FK200": "",
        }
        payload = self._kis_get_with_retry(
            path=INQUIRE_CCNL_PATH,
            tr_id=TR_ID_US_CCNL,
            params=params,
            label=f"ccnl:{pdno or 'all'}",
            error_prefix="Order ccnl inquiry failed",
        )
        output = payload.get("output") or []
        if isinstance(output, dict):
            return [output]
        if isinstance(output, list):
            return [row for row in output if isinstance(row, dict)]
        return []

    def fetch_overseas_open_orders(
        self,
        ovrs_excg_cd: str = "NASD",
        sort_sqn: str = "DS",
    ) -> list[dict[str, Any]]:
        """Fetch open (unfilled) overseas orders for an exchange bucket."""
        params = {
            "CANO": CANO,
            "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "SORT_SQN": sort_sqn,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        payload = self._kis_get_with_retry(
            path=INQUIRE_NCCS_PATH,
            tr_id=TR_ID_US_NCCS,
            params=params,
            label=f"nccs:{ovrs_excg_cd}",
            error_prefix="Open-order inquiry failed",
        )
        output = payload.get("output") or []
        if isinstance(output, dict):
            return [output]
        if isinstance(output, list):
            return [row for row in output if isinstance(row, dict)]
        return []

    def cancel_overseas_order(
        self,
        ticker: str,
        orgn_odno: str,
        quantity: int,
    ) -> dict[str, Any]:
        """Cancel an open overseas limit order."""
        ovrs_excg_cd = MARKET_META[ticker]["ovrs_excg_cd"]
        order_body = {
            "CANO": CANO,
            "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "PDNO": ticker,
            "ORGN_ODNO": orgn_odno,
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": "0",
            "MGCO_APTM_ODNO": "",
            "ORD_SVR_DVSN_CD": "0",
        }
        hashkey = self.get_hashkey(order_body)
        url = f"{BASE_URL}{ORDER_RVSECNCL_PATH}"
        response = kis_request(
            "POST",
            url,
            label=f"cancel:{ticker}",
            headers=self._build_headers(TR_ID_US_CANCEL, include_hashkey=True, hashkey=hashkey),
            json=order_body,
            timeout=KIS_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("rt_cd") not in (None, "0"):
            raise RuntimeError(
                f"Cancel rejected for {ticker}: "
                f"{payload.get('msg_cd')} | {payload.get('msg1')}"
            )
        return payload


def _parse_amount(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)


def _summarize_overseas_present_balance(payload: dict[str, Any]) -> dict[str, float]:
    summary: dict[str, float] = {
        "total_asset_krw": 0.0,
        "total_deposit_krw": 0.0,
        "withdrawable_krw": 0.0,
        "usable_foreign_cash": 0.0,
        "foreign_deposit_usd": 0.0,
        "withdrawable_foreign_usd": 0.0,
    }

    output3 = payload.get("output3") or {}
    if isinstance(output3, list):
        output3 = output3[0] if output3 else {}

    summary["total_asset_krw"] = _parse_amount(output3.get("tot_asst_amt"))
    summary["total_deposit_krw"] = _parse_amount(output3.get("tot_dncl_amt"))
    summary["withdrawable_krw"] = _parse_amount(output3.get("wdrw_psbl_tot_amt"))
    summary["usable_foreign_cash"] = _parse_amount(output3.get("frcr_use_psbl_amt"))

    output2_rows = payload.get("output2") or []
    if output2_rows:
        usd_row = next(
            (row for row in output2_rows if row.get("crcy_cd") == "USD"),
            output2_rows[0],
        )
        summary["foreign_deposit_usd"] = _parse_amount(
            usd_row.get("frcr_dncl_amt_2") or usd_row.get("frcr_drwg_psbl_amt_1")
        )
        summary["withdrawable_foreign_usd"] = _parse_amount(
            usd_row.get("frcr_drwg_psbl_amt_1")
        )

    return summary


def print_mock_account_balance(client: KISApiClient) -> None:
    print("Fetching mock account balance/deposit (inquire-present-balance)...")
    payload = client.fetch_overseas_present_balance(natn_cd="840")
    summary = _summarize_overseas_present_balance(payload)

    print("-" * 88)
    print("MOCK ACCOUNT BALANCE / DEPOSIT".center(88))
    print("-" * 88)
    print(f"Account (CANO)       : {CANO}-{ACNT_PRDT_CD}")
    print(f"TR ID                : {TR_ID_US_PRESENT_BALANCE}")
    print(f"Total Asset (KRW)    : {summary['total_asset_krw']:,.0f}")
    print(f"Total Deposit (KRW)  : {summary['total_deposit_krw']:,.0f}")
    print(f"Withdrawable (KRW)   : {summary['withdrawable_krw']:,.0f}")
    print(f"Usable Foreign Cash  : {summary['usable_foreign_cash']:,.2f}")
    print(f"Foreign Deposit (USD): {summary['foreign_deposit_usd']:,.2f}")
    print(f"Withdrawable (USD)   : {summary['withdrawable_foreign_usd']:,.2f}")
    print("-" * 88)


def log_configured_capital_model() -> None:
    print("-" * 88)
    print("CAPITAL MODEL - TICKER-ISOLATED REGIMES".center(88))
    print("-" * 88)
    print(f"Capital At Risk      : ${CAPITAL_AT_RISK:,.2f}")
    if DEPLOYMENT.is_dual:
        print(
            f"Legacy Capital Slice : ${LEGACY_CAPITAL_AT_RISK:,.2f} "
            f"({DEPLOYMENT.legacy_capital_pct:.0f}%)"
        )
        print(
            f"Top3 Capital Slice   : "
            f"${scaled_capital(CAPITAL_AT_RISK, DEPLOYMENT.top3_capital_fraction()):,.2f} "
            f"({DEPLOYMENT.top3_capital_pct:.0f}%)"
        )
    print(f"Risk Per Trade       : {RISK_PER_TRADE * 100:.1f}%")
    for ticker in WATCHLIST:
        cfg = StrategyConfigMapper.for_ticker(ticker)
        print(
            f"{ticker:<6} SMA={cfg.sma_period} RSI>={cfg.rsi_buy_threshold:.0f} "
            f"Vol={cfg.volume_threshold:.1f}x ATR={cfg.atr_multiplier:.1f} "
            f"TrendFilter={cfg.use_trend_filter}"
        )
    print(
        "Order Placement      : Not gated on balance inquiry; "
        "mock VTS suffix mismatches are ignored."
    )
    print("-" * 88)


def try_print_mock_account_balance(client: KISApiClient) -> None:
    try:
        print_mock_account_balance(client)
    except requests.RequestException as exc:
        print(f"[INFO] Balance inquiry ignored (network): {exc}")
    except Exception as exc:
        print(f"[INFO] Balance inquiry ignored: {exc}")


def _parse_kis_daily_output(rows: list[dict[str, Any]]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []

    for row in rows:
        date_raw = row.get("xymd") or row.get("stck_bsop_date")
        if not date_raw:
            continue

        date_str = pd.Timestamp(str(date_raw)).strftime("%Y-%m-%d")
        records.append(
            {
                "Date": date_str,
                "Open": float(row.get("open") or row.get("stck_oprc") or 0),
                "High": float(row.get("high") or row.get("stck_hgpr") or 0),
                "Low": float(row.get("low") or row.get("stck_lwpr") or 0),
                "Close": float(row.get("clos") or row.get("stck_clpr") or 0),
                "Volume": float(row.get("tvol") or row.get("acml_vol") or 0),
            }
        )

    if not records:
        raise ValueError("Unable to parse KIS daily price rows.")

    return pd.DataFrame(records)


def load_persisted_states(path: str = STATE_FILE) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read().strip()
        if not raw:
            print(f"[WARN] {path} is empty — starting with a fresh state registry.")
            return {}
        payload = json.loads(raw)

    if not isinstance(payload, dict):
        raise ValueError("Invalid trading state file: root must be a dictionary.")
    return payload


def save_persisted_states(states: dict[str, Any], path: str = STATE_FILE) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(states, handle, indent=2, sort_keys=True)


def _failed_order_key(signal: str, bar_date: str) -> str:
    return f"{signal}:{bar_date}"


def _resolve_execution_quantity(
    signal: str,
    proposed_size: int,
    runtime: PositionState,
) -> int:
    if signal == "BUY":
        return max(int(proposed_size), 0)

    if signal in {"SELL", "DYNAMIC_ATR_SELL"}:
        return max(int(runtime.held_quantity), 0)

    return 0


def execute_broker_order(
    client: KISApiClient,
    ticker: str,
    signal: str,
    quantity: int,
    reference_price: float,
) -> dict[str, Any]:
    if is_dry_run_mode():
        side = "BUY" if signal == "BUY" else "SELL"
        odno = f"DRY-{uuid.uuid4().hex[:8].upper()}"
        print(
            f"[DRY-RUN] {side} {ticker} | qty={quantity} | "
            f"ref={reference_price:.2f} | odno={odno} (no KIS API call)"
        )
        return {
            "rt_cd": "0",
            "dry_run": True,
            "msg1": "DRY_RUN simulated order",
            "output": {"ODNO": odno},
        }

    ovrs_excg_cd = MARKET_META[ticker]["ovrs_excg_cd"]
    use_limit = KIS_ORDER_TYPE == "limit"
    side = "BUY" if signal == "BUY" else "SELL"
    buffer_bps = limit_buffer_bps_for_ticker(ticker, EXECUTION_SETTINGS)
    order_price = (
        limit_order_price(side, reference_price, buffer_bps) if use_limit else 0.0
    )
    order_kind = "limit order" if use_limit else "market order"
    price_suffix = f" @{order_price:.2f}" if use_limit else ""

    if signal == "BUY" and quantity > 0:
        print(
            f"[KIS ORDER] BUY  {ticker} | qty={quantity} | {order_kind}{price_suffix} | "
            f"tr_id={TR_ID_US_BUY}"
        )
        return _place_order_with_retries(
            client,
            side="BUY",
            ticker=ticker,
            ovrs_excg_cd=ovrs_excg_cd,
            quantity=quantity,
            order_price=order_price,
        )

    if signal in {"SELL", "DYNAMIC_ATR_SELL"} and quantity > 0:
        print(
            f"[KIS ORDER] SELL {ticker} | qty={quantity} | close-position | "
            f"{order_kind}{price_suffix} | tr_id={TR_ID_US_SELL}"
        )
        return _place_order_with_retries(
            client,
            side="SELL",
            ticker=ticker,
            ovrs_excg_cd=ovrs_excg_cd,
            quantity=quantity,
            order_price=order_price,
        )

    print(f"[KIS ORDER] NO ACTION | {ticker} | signal={signal} | qty={quantity}")
    return {"rt_cd": "0", "skipped": True}


def _place_order_with_retries(
    client: KISApiClient,
    *,
    side: str,
    ticker: str,
    ovrs_excg_cd: str,
    quantity: int,
    order_price: float,
) -> dict[str, Any]:
    last_exc: BaseException | None = None
    for attempt in range(KIS_ORDER_MAX_RETRIES):
        try:
            return client.place_us_order(
                side=side,
                ticker=ticker,
                ovrs_excg_cd=ovrs_excg_cd,
                quantity=quantity,
                price=order_price,
            )
        except (RuntimeError, requests.RequestException) as exc:
            last_exc = exc
            if not is_retryable_request_error(exc) or attempt >= KIS_ORDER_MAX_RETRIES - 1:
                raise
            wait_seconds = KIS_ORDER_RETRY_BACKOFF_SECONDS * (2**attempt)
            print(
                f"[ORDER/RETRY] {ticker} {side} attempt {attempt + 2}/"
                f"{KIS_ORDER_MAX_RETRIES} in {wait_seconds:.1f}s — {exc}"
            )
            time.sleep(wait_seconds)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Order placement failed for {ticker}")


def _finalize_order_acceptance(
    *,
    ticker: str,
    signal: str,
    execution_qty: int,
    reference_price: float,
    order_payload: dict[str, Any],
    runtime: PositionState,
    engine: LiveSignalEngine,
    states: dict[str, Any],
    ledger: PortfolioLedger,
) -> None:
    odno = extract_order_odno(order_payload)
    side = "BUY" if signal == "BUY" else "SELL"
    buffer_bps = limit_buffer_bps_for_ticker(ticker, EXECUTION_SETTINGS)
    order_price = (
        limit_order_price(side, reference_price, buffer_bps)
        if KIS_ORDER_TYPE == "limit"
        else 0.0
    )
    submitted_at = datetime.now().isoformat(timespec="seconds")
    if odno:
        assign_open_order(
            runtime,
            odno=odno,
            side=side,
            qty=execution_qty,
            price=order_price,
            submitted_at=submitted_at,
        )
    else:
        runtime.pending_order = True
        runtime.open_order_side = side
        runtime.open_order_qty = execution_qty
        runtime.open_order_price = order_price
        runtime.open_order_submitted_at = submitted_at
        print(f"[ORDER/WARN] {ticker} accepted without odno — fill poll via ccnl/broker")

    runtime.last_failed_order_key = None
    states[ticker] = engine.dump_state(runtime)
    save_persisted_states(states)
    TRADE_LOG.append(
        ticker=ticker,
        signal=signal,
        qty=execution_qty,
        order_price=order_price or None,
        fill_price=None,
        status="ACCEPTED",
        reason=f"odno={odno or 'unknown'}",
        cash_after=ledger.available_cash_usd,
        held_qty=int(runtime.held_quantity or 0),
    )
    print(
        f"  -> Order accepted (rt_cd=0). Awaiting fill confirmation for {ticker}: "
        f"odno={odno or 'unknown'}, side={side}, qty={execution_qty}"
    )


def _apply_dry_run_fill(
    *,
    ticker: str,
    signal: str,
    execution_qty: int,
    reference_price: float,
    runtime: PositionState,
    engine: LiveSignalEngine,
    states: dict[str, Any],
    ledger: PortfolioLedger,
    current_bar_date: str,
) -> None:
    side = "BUY" if signal == "BUY" else "SELL"
    buffer_bps = limit_buffer_bps_for_ticker(ticker, EXECUTION_SETTINGS)
    fill_price = (
        limit_order_price(side, reference_price, buffer_bps)
        if KIS_ORDER_TYPE == "limit"
        else reference_price
    )

    engine.apply_post_order_transition(
        runtime,
        signal=signal,
        filled_quantity=execution_qty,
        current_bar_date=current_bar_date,
        allow_crossover=True,
    )
    if signal == "BUY":
        runtime.entry_price = fill_price
        runtime.entry_bar_date = current_bar_date
        runtime.bars_held = 0
        runtime.hold_count_bar_date = current_bar_date

    runtime.last_failed_order_key = None
    states[ticker] = engine.dump_state(runtime)
    save_persisted_states(states)
    TRADE_LOG.append(
        ticker=ticker,
        signal=signal,
        qty=execution_qty,
        order_price=fill_price,
        fill_price=fill_price,
        status="DRY_RUN",
        reason="KIS_DRY_RUN instant fill",
        cash_after=ledger.available_cash_usd,
        held_qty=int(runtime.held_quantity or 0),
    )
    _handle_trade_fill(ticker, side, execution_qty, fill_price, "DRY_RUN")
    print(
        f"[DRY-RUN/FILL] {ticker} {signal} qty={execution_qty} "
        f"@ {fill_price:.2f} — state updated immediately"
    )


def dispatch_order_with_state_machine(
    client: KISApiClient,
    engine: LiveSignalEngine,
    ticker: str,
    runtime: PositionState,
    signal: str,
    proposed_size: int,
    current_bar_date: str,
    allow_crossover: bool,
    states: dict[str, Any],
    reference_price: float,
    ledger: PortfolioLedger,
) -> str:
    """
    Enforce order lifecycle:
      pending lock -> cooldown -> dispatch -> store odno -> fill poll updates state
    """
    if runtime.pending_order:
        print(f"[LOCK] {ticker} pending_order=True — signal suppressed")
        return "LOCKED"

    execution_qty = _resolve_execution_quantity(signal, proposed_size, runtime)
    if signal in {"SELL", "DYNAMIC_ATR_SELL"} and execution_qty <= 0:
        print(f"[SKIP] {ticker} liquidation skipped — no held quantity tracked")
        runtime.in_position = False
        return "HOLD"

    if signal == "BUY" and execution_qty <= 0:
        print(f"[SKIP] {ticker} BUY skipped — zero share size")
        return "HOLD"

    if signal not in {"BUY", "SELL", "DYNAMIC_ATR_SELL"}:
        return signal

    fail_key = _failed_order_key(signal, current_bar_date)
    if runtime.last_failed_order_key == fail_key:
        print(
            f"[ORDER/SKIP] {ticker} — prior rejection on {fail_key}; "
            "waiting for new bar"
        )
        return "HOLD"

    try:
        order_payload = execute_broker_order(
            client,
            ticker,
            signal,
            execution_qty,
            reference_price,
        )
    except (RuntimeError, requests.RequestException) as exc:
        runtime.last_failed_order_key = fail_key
        states[ticker] = engine.dump_state(runtime)
        save_persisted_states(states)
        TRADE_LOG.append(
            ticker=ticker,
            signal=signal,
            qty=execution_qty,
            order_price=None,
            fill_price=None,
            status="REJECTED",
            reason=str(exc),
            cash_after=ledger.available_cash_usd,
            held_qty=int(runtime.held_quantity or 0),
        )
        print(f"[ORDER/REJECTED] {ticker} {signal} qty={execution_qty} — {exc}")
        if is_retryable_request_error(exc):
            ORDER_RETRY_QUEUE.enqueue(
                PendingOrderRetry(
                    ticker=ticker,
                    signal=signal,
                    quantity=execution_qty,
                    reference_price=reference_price,
                    fail_key=fail_key,
                    current_bar_date=current_bar_date,
                    last_error=str(exc),
                )
            )
        return "HOLD"

    if order_payload.get("skipped"):
        return signal

    if str(order_payload.get("rt_cd", "")) != "0":
        runtime.last_failed_order_key = fail_key
        states[ticker] = engine.dump_state(runtime)
        save_persisted_states(states)
        TRADE_LOG.append(
            ticker=ticker,
            signal=signal,
            qty=execution_qty,
            order_price=None,
            fill_price=None,
            status="REJECTED",
            reason=f"rt_cd={order_payload.get('rt_cd')}",
            cash_after=ledger.available_cash_usd,
            held_qty=int(runtime.held_quantity or 0),
        )
        print(
            f"[ORDER/REJECTED] {ticker} {signal} qty={execution_qty} — "
            f"rt_cd={order_payload.get('rt_cd')}"
        )
        return "HOLD"

    if order_payload.get("dry_run"):
        _apply_dry_run_fill(
            ticker=ticker,
            signal=signal,
            execution_qty=execution_qty,
            reference_price=reference_price,
            runtime=runtime,
            engine=engine,
            states=states,
            ledger=ledger,
            current_bar_date=current_bar_date,
        )
        return signal

    odno = extract_order_odno(order_payload)
    _finalize_order_acceptance(
        ticker=ticker,
        signal=signal,
        execution_qty=execution_qty,
        reference_price=reference_price,
        order_payload=order_payload,
        runtime=runtime,
        engine=engine,
        states=states,
        ledger=ledger,
    )
    return "PENDING"


def _analytics_sma20(df: pd.DataFrame) -> float:
    if len(df) < ANALYTICS_SMA_PERIOD:
        return float("nan")
    return float(df["Close"].rolling(window=ANALYTICS_SMA_PERIOD).mean().iloc[-1])


def print_session_telemetry(
    ticker: str,
    cycle: dict[str, Any],
    df: pd.DataFrame,
) -> None:
    metrics = cycle["metrics"]["today"]
    signal = cycle["signal_result"]["signal"]
    close = float(metrics["close"])
    sma_20 = _analytics_sma20(df)
    rsi = float(metrics["rsi"])
    atr = float(metrics["atr"])
    volume = float(metrics["volume"])
    volume_avg_20 = float(metrics["volume_sma"])
    volume_ratio = volume / volume_avg_20 if volume_avg_20 > 0 else 0.0
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    api_ms = last_api_response_ms()
    api_suffix = f" | api_response_time={api_ms:.0f}ms" if api_ms is not None else ""
    print("-" * 88)
    print(
        f"[METRICS] {ticker} | {timestamp} | "
        f"Close={close:.2f} | SMA20={sma_20:.2f} | RSI={rsi:.2f} | "
        f"ATR_Wilder={atr:.4f} | Volume={volume:,.0f}/{volume_avg_20:,.0f} "
        f"({volume_ratio:.3f}x){api_suffix}"
    )
    print(
        f"[{ticker}] Current Close | 20 SMA | Standard RSI | Wilder's ATR | "
        f"Current Volume / 20-day Avg Volume"
    )
    print(
        f"[{ticker}] {close:.2f} | {sma_20:.2f} | {rsi:.2f} | {atr:.4f} | "
        f"{volume:,.0f} / {volume_avg_20:,.0f}"
    )
    print(f"Session Date         : {metrics['date']}")
    print(f"Bar Date             : {cycle['current_bar_date']}")
    print(f"Session Low          : {cycle['session_low']:.4f}")
    print(f"Calendar Open        : {cycle.get('calendar_open', cycle.get('market_open'))}")
    print(f"Regular Market Hours : {cycle.get('regular_market_hours', cycle.get('market_open'))}")
    print(f"Crossover Allowed    : {cycle['allow_crossover']}")
    print(
        f"SPY Regime (> {BENCHMARK_SMA_PERIOD}MA): "
        f"{'BULL' if cycle.get('market_bullish', True) else 'BEAR'}"
    )
    if cycle.get("spy_close") is not None:
        spy_sma = cycle.get("spy_sma")
        sma_text = f"{spy_sma:.2f}" if spy_sma is not None else "N/A"
        print(
            f"SPY Snapshot         : close={cycle['spy_close']:.2f} | "
            f"{BENCHMARK_SMA_PERIOD}MA={sma_text}"
        )
    print(f"Signal               : {signal}")
    print(f"Position Size (shares): {cycle['position_size']}")
    print(f"Liquidity OK         : {cycle['signal_result'].get('liquidity_ok', 'N/A')}")
    runtime = cycle["runtime_state"]
    print(
        f"Runtime Registry     : pending={runtime.get('pending_order')} | "
        f"held_qty={runtime.get('held_quantity')} | "
        f"last_processed={runtime.get('last_processed_date')}"
    )

    trigger_floor = runtime.get("trigger_floor")
    if trigger_floor is not None:
        try:
            floor_value = float(trigger_floor)
            distance = close - floor_value
            print(
                f"Trigger Floor Dist   : {distance:.4f} "
                f"(close={close:.2f} - floor={floor_value:.2f})"
            )
        except (TypeError, ValueError):
            pass

    if cycle.get("eod_atr_stops"):
        print("ATR Stop Mode        : EOD (USE_EOD_ATR_STOPS=true — backtest parity)")

    if signal == "DYNAMIC_ATR_SELL":
        stop = cycle["signal_result"].get("dynamic_atr_stop") or {}
        print(
            f"ATR Stop Telemetry   : peak=${stop.get('captured_peak', 0):.2f} | "
            f"floor=${stop.get('trigger_floor', 0):.2f} | "
            f"session_low=${stop.get('session_low', cycle['session_low']):.2f}"
        )
    if cycle["signal_result"].get("intraday_atr_scan"):
        print("Intraday ATR Scan    : ACTIVE (session_low vs prior trigger_floor)")
    if cycle["signal_result"].get("blocked_signal"):
        print(
            f"RTH Blocked Signal   : {cycle['signal_result'].get('blocked_signal')} "
            f"(pre/post-market gate)"
        )
    if cycle.get("available_capital") is not None:
        print(f"Deployable Cash      : ${cycle['available_capital']:,.2f}")
    if cycle.get("portfolio_equity") is not None:
        print(f"Portfolio Equity     : ${cycle['portfolio_equity']:,.2f}")


def _estimate_portfolio_equity(
    ledger: PortfolioLedger,
    states: dict[str, Any],
    cache: MarketDataCache,
) -> float:
    """Mark-to-market equity: broker cash + open position notionals."""
    equity = ledger.available_cash_usd
    for ticker in WATCHLIST:
        ticker_state = states.get(ticker, {})
        shares = int(ticker_state.get("held_quantity", 0) or 0)
        if shares <= 0:
            continue
        try:
            frame = cache.get_frame(ticker)
            mark = float(frame.iloc[-1]["Close"])
            equity += shares * mark
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return max(equity, ledger.available_cash_usd)


def _should_run_session_reconciliation(states: dict[str, Any], now: datetime) -> bool:
    """Reconcile once per calendar day at the first RTH cycle."""
    if not is_us_regular_market_hours(now):
        return False
    portfolio_meta = states.get("_portfolio", {})
    last_session_date = portfolio_meta.get("last_reconcile_session_date")
    today = now.strftime("%Y-%m-%d")
    return last_session_date != today


def run_session_reconciliation(
    client: KISApiClient,
    states: dict[str, Any],
    *,
    force: bool = False,
) -> tuple[dict[str, Any], PortfolioLedger]:
    """Broker-vs-local sync with automatic override on quantity mismatch."""
    now = datetime.now()
    if not force and not _should_run_session_reconciliation(states, now):
        cached = states.get("_portfolio", {})
        ledger = PortfolioLedger(
            broker_cash_usd=float(cached.get("broker_cash_usd", 0.0) or 0.0),
            available_cash_usd=float(
                cached.get("available_cash_usd", CAPITAL_AT_RISK) or CAPITAL_AT_RISK
            ),
            last_reconciled_at=cached.get("last_reconciled_at"),
        )
        return states, ledger

    print("[RECONCILE] Starting broker portfolio synchronization...")
    states, ledger, report = _reconciliation_engine.reconcile(
        client,
        states,
        fallback_cash=CAPITAL_AT_RISK,
    )
    if not report.reconciled and report.error:
        level = (
            "WARNING"
            if _is_transient_kis_failure(RuntimeError(report.error))
            or "500 Server Error" in str(report.error)
            else "CRITICAL"
        )
        _dispatch_system_alert(
            level,
            f"Broker reconciliation failed: {report.error}",
        )
    if report.reconciled and is_us_regular_market_hours(now):
        states.setdefault("_portfolio", {})["last_reconcile_session_date"] = now.strftime(
            "%Y-%m-%d"
        )
    save_persisted_states(states)
    return states, ledger


def _ticker_in_active_momentum(states: dict[str, Any], ticker: str) -> bool:
    portfolio = states.get("_portfolio", {})
    if not isinstance(portfolio, dict):
        return False
    active = portfolio.get("active_trade_tickers") or []
    if not isinstance(active, list):
        return False
    return ticker.upper() in {str(t).upper() for t in active}


def process_ticker(
    client: KISApiClient,
    cache: MarketDataCache,
    ticker: str,
    states: dict[str, Any],
    ledger: PortfolioLedger,
    *,
    active_trade_tickers: frozenset[str] | None = None,
    market_frame: pd.DataFrame | None = None,
    is_new_bar: bool = False,
    cycle_timestamp: datetime | None = None,
) -> str:
    """Run fetch-evaluate-execute-persist cycle for one ticker with state machine gates."""
    engine = LiveSignalEngine(ticker)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Processing ticker: {ticker}")

    if market_frame is None:
        market_frame, is_new_bar = cache.refresh_latest(ticker, now=cycle_timestamp)
    df = market_frame
    data_source = cache.get_data_source(ticker)
    refresh_kind = "new daily bar" if is_new_bar else "intraday overlay"
    print(
        f"  -> {len(df)} bars in memory ({refresh_kind}) [source={data_source}]"
    )

    ticker_state = states.get(ticker, {})
    if not isinstance(ticker_state, dict):
        ticker_state = {}

    current_price = float(df.iloc[-1]["Close"])
    current_bar_date = pd.Timestamp(df.iloc[-1]["Date"]).strftime("%Y-%m-%d")

    runtime = engine.load_state(ticker_state)
    if runtime.pending_order or runtime.open_order_id:
        broker_qty = int(
            states.get("_portfolio", {})
            .get("broker_holdings", {})
            .get(ticker, runtime.held_quantity)
            or 0
        )
        FILL_MONITOR.resolve_ticker(
            client,
            engine,
            ticker,
            runtime,
            states,
            broker_qty=broker_qty,
            cash_after=ledger.available_cash_usd,
            current_bar_date=current_bar_date,
        )
        states[ticker] = engine.dump_state(runtime)
        save_persisted_states(states)
        if runtime.pending_order:
            print(f"  -> Pending order still open for {ticker}")
            return "PENDING"
        ticker_state = states.get(ticker, engine.dump_state(runtime))

    portfolio_equity = _estimate_portfolio_equity(ledger, states, cache)
    RISK_GUARD.update_daily_equity_anchor(states, portfolio_equity)

    legacy_fraction = DEPLOYMENT.legacy_capital_fraction()
    effective_capital = (
        LEGACY_CAPITAL_AT_RISK if DEPLOYMENT.is_dual else CAPITAL_AT_RISK
    )
    effective_cash = (
        ledger.available_cash_usd * legacy_fraction
        if DEPLOYMENT.is_dual
        else ledger.available_cash_usd
    )

    market_bullish = True
    position_size_multiplier = 1.0
    market_regime_label = "normal"
    spy_close: float | None = None
    spy_sma: float | None = None
    if USE_SPY_MARKET_FILTER:
        try:
            spy_df = cache.get_frame(BENCHMARK_TICKER)
            qqq_df = None
            if USE_QQQ_REGIME_FILTER:
                try:
                    qqq_df = cache.get_frame(SECONDARY_BENCHMARK_TICKER)
                except KeyError:
                    print(f"[WARN] {SECONDARY_BENCHMARK_TICKER} not bootstrapped — QQQ regime skipped")
            regime, spy_close, spy_sma = market_regime_snapshot(
                spy_df,
                qqq_df,
                current_bar_date,
                StrategyConfigMapper.MARKET_BENCHMARK_SMA_PERIOD,
            )
            market_bullish = regime.allow_new_buys
            position_size_multiplier = regime.position_size_multiplier
            market_regime_label = regime.label
        except KeyError:
            print(f"[WARN] {BENCHMARK_TICKER} not bootstrapped — market filter skipped")

    cycle = engine.evaluate_trading_cycle(
        df,
        runtime_state=states.get(ticker, ticker_state if isinstance(ticker_state, dict) else {}),
        capital_at_risk=effective_capital,
        risk_per_trade=RISK_PER_TRADE,
        now=datetime.now(),
        available_capital=effective_cash,
        portfolio_equity=portfolio_equity * legacy_fraction
        if DEPLOYMENT.is_dual
        else portfolio_equity,
        current_price=current_price,
        market_bullish=market_bullish,
        position_size_multiplier=position_size_multiplier,
        momentum_ranked_hold=_ticker_in_active_momentum(states, ticker),
    )
    cycle["market_regime"] = market_regime_label
    cycle["position_size_multiplier"] = position_size_multiplier
    cycle["spy_close"] = spy_close
    cycle["spy_sma"] = spy_sma
    cycle["available_capital"] = effective_cash
    cycle["portfolio_equity"] = (
        portfolio_equity * legacy_fraction if DEPLOYMENT.is_dual else portfolio_equity
    )

    runtime = engine.load_state(cycle["runtime_state"])
    _execution_gatekeeper.evaluate_live_signals(
        engine,
        runtime,
        cycle,
        current_price=current_price,
    )
    cycle["runtime_state"] = runtime.to_dict()

    print_session_telemetry(ticker, cycle, df)

    runtime = engine.load_state(cycle["runtime_state"])
    signal = cycle["signal_result"]["signal"]
    proposed_size = int(cycle["position_size"])

    if cycle["signal_result"].get("pending_order_locked"):
        print(f"[LOCK] {ticker} pending order lock active — no dispatch")
        states[ticker] = engine.dump_state(runtime)
        save_persisted_states(states)
        return "LOCKED"

    regular_market_hours = bool(
        cycle.get("regular_market_hours", cycle.get("market_open", False))
    )
    if _rth_gate.block_signal_for_session(signal, regular_market_hours):
        print(_rth_gate.gate_message(signal, ticker))
        states[ticker] = engine.dump_state(runtime)
        save_persisted_states(states)
        return "HOLD"

    if signal in {"SELL", "DYNAMIC_ATR_SELL"} and runtime.held_quantity <= 0:
        print(
            f"[SKIP] {ticker} {signal} suppressed — held_qty=0 "
            "(no broker shares)"
        )
        runtime.in_position = False
        states[ticker] = engine.dump_state(runtime)
        save_persisted_states(states)
        return "HOLD"

    if signal in {"BUY", "SELL", "DYNAMIC_ATR_SELL"}:
        if signal == "BUY" and data_source == "yfinance":
            print(
                f"[GATE/DATA] {ticker} BUY blocked — bar history loaded via yfinance fallback"
            )
            states[ticker] = engine.dump_state(runtime)
            save_persisted_states(states)
            return "HOLD"

        if signal == "BUY":
            if position_size_multiplier <= 0:
                print(f"[GATE/REGIME] {ticker} BUY blocked — market risk-off")
                states[ticker] = engine.dump_state(runtime)
                save_persisted_states(states)
                return "HOLD"

            if active_trade_tickers is not None and not is_new_buy_allowed(
                ticker,
                active_trade_tickers,
                settings=MOMENTUM_SETTINGS,
            ):
                print(
                    f"[GATE/MOMENTUM] {ticker} BUY blocked — outside Top "
                    f"{MOMENTUM_SETTINGS.top_n} momentum rank"
                )
                states[ticker] = engine.dump_state(runtime)
                save_persisted_states(states)
                return "HOLD"

            rth_block = block_new_buy_rth_window(datetime.now(), EXECUTION_SETTINGS)
            if rth_block:
                print(f"[GATE/RTH] {ticker} BUY blocked — {rth_block}")
                states[ticker] = engine.dump_state(runtime)
                save_persisted_states(states)
                return "HOLD"

            risk_block = RISK_GUARD.check_buy_allowed(
                ticker,
                proposed_size,
                current_price,
                states,
                now=datetime.now(),
            )
            if risk_block:
                print(f"[GATE/RISK] {ticker} BUY blocked — {risk_block}")
                states[ticker] = engine.dump_state(runtime)
                save_persisted_states(states)
                return "HOLD"

        execution_qty = _resolve_execution_quantity(signal, proposed_size, runtime)
        final_signal = dispatch_order_with_state_machine(
            client,
            engine,
            ticker,
            runtime,
            signal,
            proposed_size,
            cycle["current_bar_date"],
            cycle["allow_crossover"],
            states,
            reference_price=current_price,
            ledger=ledger,
        )
        if final_signal == "PENDING":
            broker_qty = int(
                states.get("_portfolio", {})
                .get("broker_holdings", {})
                .get(ticker, runtime.held_quantity)
                or 0
            )
            FILL_MONITOR.resolve_ticker(
                client,
                engine,
                ticker,
                runtime,
                states,
                broker_qty=broker_qty,
                cash_after=ledger.available_cash_usd,
                current_bar_date=cycle["current_bar_date"],
            )
            states[ticker] = engine.dump_state(runtime)
            save_persisted_states(states)
            if runtime.pending_order:
                return "PENDING"
            return signal
    else:
        final_signal = signal
        states[ticker] = engine.dump_state(runtime)
        save_persisted_states(states)

    print(f"  -> State persisted for {ticker}")
    return final_signal


def process_order_retry_queue(
    client: KISApiClient,
    states: dict[str, Any],
    ledger: PortfolioLedger,
) -> None:
    """Retry queued orders from prior transient KIS failures."""

    def _executor(item: PendingOrderRetry) -> dict[str, Any]:
        engine = LiveSignalEngine(item.ticker)
        runtime = engine.load_state(states.get(item.ticker, {}))
        payload = execute_broker_order(
            client,
            item.ticker,
            item.signal,
            item.quantity,
            item.reference_price,
        )
        if str(payload.get("rt_cd", "")) == "0" and not payload.get("skipped"):
            _finalize_order_acceptance(
                ticker=item.ticker,
                signal=item.signal,
                execution_qty=item.quantity,
                reference_price=item.reference_price,
                order_payload=payload,
                runtime=runtime,
                engine=engine,
                states=states,
                ledger=ledger,
            )
        return payload

    retried = ORDER_RETRY_QUEUE.process_due(_executor)
    if retried:
        print(f"[ORDER/QUEUE] Completed {retried} queued retry submission(s)")


def run_top3_shadow_cycle(
    client: KISApiClient,
    cache: MarketDataCache,
    states: dict[str, Any],
    ledger: PortfolioLedger,
) -> None:
    """Phase 3 shadow or Phase 4 live-split Top3 momentum rebalance."""
    if not DEPLOYMENT.top3_shadow_active:
        return

    portfolio_equity = _estimate_portfolio_equity(ledger, states, cache)
    shadow = load_top3_state(states)
    shadow, orders, logs = compute_top3_rebalance_orders(
        cache,
        WATCHLIST,
        shadow,
        total_equity_usd=portfolio_equity,
        deploy=DEPLOYMENT,
        now=datetime.now(),
        settings=_MOMENTUM_RAW,
    )

    if not logs and not orders:
        return

    print()
    print("-" * 88)
    print("TOP3 STRATEGY CYCLE".center(88))
    print("-" * 88)
    for line in logs:
        print(line)

    if DEPLOYMENT.top3_live_orders:
        for order in orders:
            execute_broker_order(
                client,
                order.ticker,
                order.side,
                order.shares,
                order.reference_price,
            )
    elif orders:
        print(
            f"[TOP3/SHADOW] {len(orders)} simulated order(s) — "
            "no KIS dispatch (Phase 3 dry-run)"
        )
        if _telegram_enabled():
            summary = ", ".join(
                f"{o.side} {o.shares} {o.ticker}" for o in orders[:5]
            )
            _run_telegram(
                send_system_alert(
                    "INFO",
                    f"Top3 shadow rebalance: {summary}",
                )
            )

    save_top3_state(states, shadow)
    print("-" * 88)


def run_watchlist_cycle(
    client: KISApiClient,
    cache: MarketDataCache,
    states: dict[str, Any],
    ledger: PortfolioLedger,
) -> tuple[dict[str, Any], PortfolioLedger]:
    summary: list[tuple[str, str]] = []

    if not is_us_equity_session():
        closure_reason = describe_us_market_closure() or "market closed"
        print(
            f"[GATE] US calendar session closed ({closure_reason}) — "
            "no live execution; sleeping"
        )
        return states, ledger

    if not is_us_regular_market_hours():
        print(
            "[GATE/RTH] Outside NY regular hours (09:30-16:00 ET) — "
            "skipping API cycle until RTH open"
        )
        return states, ledger

    states, ledger = run_session_reconciliation(client, states)
    FILL_MONITOR.resolve_all_pending(
        client,
        states,
        WATCHLIST,
        LiveSignalEngine,
        cash_after=ledger.available_cash_usd,
    )
    save_persisted_states(states)
    process_order_retry_queue(client, states, ledger)

    momentum_snapshot = rebalance_active_tickers(
        cache,
        WATCHLIST,
        states,
        settings=MOMENTUM_SETTINGS,
    )
    active_trade_tickers = frozenset(momentum_snapshot.active_tickers)
    cycle_tickers = build_cycle_tickers(
        WATCHLIST,
        momentum_snapshot.active_tickers,
        states,
    )
    if MOMENTUM_SETTINGS.enabled:
        skipped = [t for t in WATCHLIST if t not in cycle_tickers]
        if skipped:
            print(
                f"[MOMENTUM] Skipping flat tickers outside Top "
                f"{MOMENTUM_SETTINGS.top_n}: {', '.join(skipped)}"
            )

    refresh_tickers = list(cycle_tickers)
    if USE_SPY_MARKET_FILTER:
        refresh_tickers.append(BENCHMARK_TICKER)
    if USE_QQQ_REGIME_FILTER:
        refresh_tickers.append(SECONDARY_BENCHMARK_TICKER)
    cycle_timestamp = datetime.now()
    refresh_map = cache.refresh_latest_parallel(refresh_tickers, now=cycle_timestamp)
    print(
        f"[CYCLE] Refreshed {len(refresh_map)} tickers in parallel "
        f"at {cycle_timestamp.strftime('%H:%M:%S')}"
    )

    for ticker in cycle_tickers:
        frame, is_new = refresh_map.get(
            ticker,
            (cache.evaluation_frame(ticker), False),
        )
        try:
            signal = process_ticker(
                client,
                cache,
                ticker,
                states,
                ledger,
                active_trade_tickers=active_trade_tickers,
                market_frame=frame,
                is_new_bar=is_new,
                cycle_timestamp=cycle_timestamp,
            )
            summary.append((ticker, signal))
        except requests.Timeout as exc:
            print(f"[ERROR] Timeout for {ticker}: {exc}")
            _dispatch_system_alert("WARNING", f"{ticker} KIS request timeout: {exc}")
            summary.append((ticker, "TIMEOUT"))
        except requests.RequestException as exc:
            print(f"[ERROR] Network failure for {ticker}: {exc}")
            alert_level = "CRITICAL" if _is_auth_failure(exc) else "WARNING"
            _dispatch_system_alert(
                alert_level,
                f"{ticker} KIS API connection failure: {exc}",
            )
            summary.append((ticker, "NETWORK_ERROR"))
        except Exception as exc:
            print(f"[ERROR] Unhandled failure for {ticker}: {exc}")
            traceback.print_exc()
            _dispatch_system_alert(
                "CRITICAL",
                f"{ticker} unhandled trading loop failure: {exc}",
            )
            summary.append((ticker, "FAILED"))

    run_top3_shadow_cycle(client, cache, states, ledger)
    cache.maybe_collect_garbage()
    save_persisted_states(states)

    print()
    print("=" * 88)
    print("WATCHLIST CYCLE SUMMARY".center(88))
    print("=" * 88)
    for ticker, status in summary:
        print(f"  {ticker:<6} -> {status}")
    print("=" * 88)

    return states, ledger


def _maybe_send_eod_report(
    states: dict[str, Any],
    ledger: PortfolioLedger,
    *,
    now: datetime | None = None,
) -> None:
    if not should_send_eod_report(now, states):
        return

    metrics = compile_eod_metrics(
        states,
        WATCHLIST,
        trade_log_path=TRADE_LOG_FILE,
        available_cash=ledger.available_cash_usd,
        now=now,
    )
    text = format_eod_report_text(metrics)
    print(f"[EOD] Sending daily report for {metrics['date']}...")
    if _telegram_enabled():
        _run_telegram(send_eod_report(text))
    else:
        print(text.replace("\\", ""))
    mark_eod_report_sent(states)
    save_persisted_states(states)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        validate_environment()
    except RuntimeError as exc:
        _dispatch_system_alert("CRITICAL", f"Startup validation failed: {exc}")
        raise

    print("KIS Mock Trading - Production Execution Engine")
    print(f"Execution Timestamp : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Deployment          : {DEPLOYMENT.describe()}")
    print(f"Base URL            : {BASE_URL}")
    print(f"Account             : {CANO}-{ACNT_PRDT_CD}")
    print(f"Watchlist           : {', '.join(WATCHLIST)}")
    print(
        f"Momentum Rank       : "
        f"{'ON (Top ' + str(MOMENTUM_SETTINGS.top_n) + ' on Friday)' if MOMENTUM_SETTINGS.enabled else 'OFF'}"
    )
    print(
        f"SPY Market Filter   : "
        f"{'ON (regime gate)' if USE_SPY_MARKET_FILTER else 'OFF'}"
    )
    print(
        f"QQQ Regime Filter   : "
        f"{'ON (half size when SPY<200MA & QQQ>200MA)' if USE_QQQ_REGIME_FILTER else 'OFF'}"
    )
    print(f"Capital At Risk     : ${CAPITAL_AT_RISK:,.2f}")
    if DEPLOYMENT.is_dual:
        print(f"Legacy Sizing Pool  : ${LEGACY_CAPITAL_AT_RISK:,.2f}")
        print(
            f"Top3 Strategy       : "
            f"{'shadow dry-run' if DEPLOYMENT.phase == 3 else 'live split'}"
        )
    print(
        f"KIS Order Type      : {KIS_ORDER_TYPE.upper()} "
        f"(default buffer={EXECUTION_SETTINGS.default_limit_buffer_bps} bps, "
        f"high-vol={EXECUTION_SETTINGS.high_vol_limit_buffer_bps} bps)"
    )
    print(f"Trade Log File      : {TRADE_LOG_FILE}")
    print(
        f"Risk Limits         : daily_loss=${EXECUTION_SETTINGS.max_daily_loss_usd:.0f}, "
        f"max_positions={EXECUTION_SETTINGS.max_open_positions}, "
        f"max_exposure=${EXECUTION_SETTINGS.max_ticker_exposure_usd:.0f}/ticker"
    )
    print(f"State File          : {STATE_FILE}")
    print(f"Ticker Interval     : {TICKER_SLEEP_SECONDS}s (order pacing; OHLCV refresh is parallel)")
    parallel_refresh = os.getenv("PARALLEL_TICKER_REFRESH", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    workers = os.getenv("TICKER_REFRESH_WORKERS", "3")
    stagger = os.getenv("KIS_REFRESH_STAGGER_SECONDS", "0.35")
    print(
        f"OHLCV Refresh       : "
        f"{'parallel' if parallel_refresh else 'sequential+stagger'} "
        f"(workers={workers}, stagger={stagger}s, forming-bar overlay)"
    )
    print(f"Loop Cooldown       : {LOOP_COOLDOWN_SECONDS}s")
    print(f"Market Closed Sleep : {MARKET_CLOSED_SLEEP_SECONDS}s")
    print(f"KIS Request Timeout : {KIS_REQUEST_TIMEOUT_SECONDS}s")
    print(f"KIS Slow API Warn   : {KIS_SLOW_API_MS}ms")
    print(
        f"Order Retry Policy  : inline={KIS_ORDER_MAX_RETRIES}x, "
        f"queue={ORDER_RETRY_QUEUE.path}, backoff={KIS_ORDER_RETRY_BACKOFF_SECONDS}s"
    )
    print(
        f"ATR Stop Mode       : "
        f"{'EOD (backtest parity)' if use_eod_atr_stops() else 'Intraday session_low'}"
    )
    print(
        f"Telegram Alerts     : "
        f"{'ON' if _telegram_enabled() else 'OFF (USE_TELEGRAM_ALERTS=false)'}"
    )
    print(
        f"EOD Telegram Report : "
        f"{'ON (after 16:00 ET)' if use_daily_telegram_report() else 'OFF'}"
    )
    print(
        "Extended Hours      : OFF (daily-bar strategy; pre/post market orders disabled)"
    )
    off_hours_sleep = int(os.getenv("OFF_HOURS_MAX_SLEEP_SECONDS", "7200"))
    print(f"Off-Hours Sleep Cap : {off_hours_sleep}s (adaptive until 09:30 ET)")
    if is_dry_run_mode():
        print("*** KIS DRY-RUN MODE — orders simulated locally, no broker API ***")
    print("=" * 88)

    client = KISApiClient()
    cache = MarketDataCache(
        client,
        market_meta=MARKET_META,
        data_dir=DATA_DIR,
        target_bars=TARGET_BARS,
        min_data_bars=MIN_DATA_BARS,
    )

    print("Requesting KIS access token...")
    try:
        client.get_access_token()
    except Exception as exc:
        _dispatch_system_alert("CRITICAL", f"KIS authentication failed: {exc}")
        raise
    print("Access token ready.")

    print("Bootstrapping historical market data cache (one-time full fetch)...")
    bootstrap_tickers = list(WATCHLIST)
    if USE_SPY_MARKET_FILTER and BENCHMARK_TICKER not in bootstrap_tickers:
        bootstrap_tickers.append(BENCHMARK_TICKER)
    if USE_QQQ_REGIME_FILTER and SECONDARY_BENCHMARK_TICKER not in bootstrap_tickers:
        bootstrap_tickers.append(SECONDARY_BENCHMARK_TICKER)
    cache.bootstrap(bootstrap_tickers)

    states = load_persisted_states()
    print(f"Loaded state entries: {len(states)}")

    try_print_mock_account_balance(client)
    log_configured_capital_model()

    print("[P3] Running startup portfolio reconciliation...")
    states, ledger = run_session_reconciliation(client, states, force=True)
    FILL_MONITOR.resolve_all_pending(
        client,
        states,
        WATCHLIST,
        LiveSignalEngine,
        cash_after=ledger.available_cash_usd,
    )
    save_persisted_states(states)

    if MOMENTUM_SETTINGS.enabled:
        print("[MOMENTUM] Seeding Top-N active trade universe at startup...")
        rebalance_active_tickers(
            cache,
            WATCHLIST,
            states,
            settings=MOMENTUM_SETTINGS,
            force=True,
        )
        save_persisted_states(states)

    print("Starting production monitoring loop. Press Ctrl+C to stop.")
    print(
        "Order pipeline: reconcile -> fill poll -> RTH/risk gates -> dispatch -> trade_log"
    )
    print("=" * 88)

    cycle_count = 0

    try:
        while True:
            cycle_count += 1
            cycle_started = datetime.now()
            closure_reason = describe_us_market_closure(cycle_started)

            if closure_reason is not None:
                print()
                print(
                    f"--- Cycle {cycle_count} skipped at "
                    f"{cycle_started.strftime('%Y-%m-%d %H:%M:%S')} ---"
                )
                print(
                    f"[GATE] US market closed ({closure_reason}). "
                    f"Sleeping {MARKET_CLOSED_SLEEP_SECONDS} seconds..."
                )
                time.sleep(MARKET_CLOSED_SLEEP_SECONDS)
                continue

            if not is_us_regular_market_hours(cycle_started):
                finalized = cache.finalize_intraday_bars(now=cycle_started)
                if finalized:
                    print(
                        f"[CACHE] Finalized {finalized} forming bar(s) into "
                        "completed EOD history (disk-safe)"
                    )
                _maybe_send_eod_report(states, ledger, now=cycle_started)
                sleep_seconds = seconds_until_us_rth_open(cycle_started)
                print()
                print(
                    f"--- Cycle {cycle_count} skipped at "
                    f"{cycle_started.strftime('%Y-%m-%d %H:%M:%S')} ---"
                )
                print(
                    "[GATE/RTH] Outside NY regular hours (09:30-16:00 ET) — "
                    f"no KIS API calls; sleeping {sleep_seconds}s until next RTH window"
                )
                time.sleep(sleep_seconds)
                continue

            print()
            print(
                f"--- Cycle {cycle_count} started at "
                f"{cycle_started.strftime('%Y-%m-%d %H:%M:%S')} ---"
            )

            try:
                states, ledger = run_watchlist_cycle(client, cache, states, ledger)
            except Exception as exc:
                if _is_transient_kis_failure(exc):
                    print(f"[WARN] Cycle skipped after transient KIS error: {exc}")
                    _dispatch_system_alert(
                        "WARNING",
                        f"Cycle skipped (transient KIS): {str(exc)[:200]}",
                    )
                    time.sleep(LOOP_COOLDOWN_SECONDS)
                    continue
                raise

            print(
                f"Cycle {cycle_count} complete. "
                f"Cooling down for {LOOP_COOLDOWN_SECONDS} seconds..."
            )
            time.sleep(LOOP_COOLDOWN_SECONDS)

    except KeyboardInterrupt:
        print()
        print("KeyboardInterrupt received. Shutting down gracefully...")
        save_persisted_states(states)
        print(f"Final state saved to {STATE_FILE}")
        sys.exit(0)
    except Exception as exc:
        if _is_transient_kis_failure(exc):
            print(f"[FATAL] Unrecoverable after transient errors: {exc}")
        print(f"[FATAL] Trading loop crashed: {exc}")
        traceback.print_exc()
        if not _is_transient_kis_failure(exc):
            _dispatch_system_alert("CRITICAL", f"Trading loop crashed: {exc}")
        save_persisted_states(states)
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if not isinstance(exc, KeyboardInterrupt):
            logger.critical("Bot terminated: %s", exc)
        raise
