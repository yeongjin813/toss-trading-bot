"""
Production execution loop — Korea Investment & Securities (KIS) mock trading API.

Setup:
  Create a `.env` file in the project root with the following keys:

    KIS_APP_KEY=your_app_key
    KIS_APP_SECRET=your_app_secret
    KIS_CANO=your_account_number
    KIS_ACNT_PRDT_CD=02

Run:
  python main.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

from analytics import LiveSignalEngine, StrategyConfig

load_dotenv(override=True)

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
CANO = os.getenv("KIS_CANO")
ACNT_PRDT_CD = os.getenv("KIS_ACNT_PRDT_CD")

BASE_URL = "https://openapivts.koreainvestment.com:29443"

TR_ID_US_BUY = "VTTT1002U"
TR_ID_US_SELL = "VTTT1006U"
TR_ID_DAILY_PRICE = "HHDFS76240000"
# Overseas mock present-balance inquiry (v1_해외주식-008).
# Note: VTTT3402R is rejected by the VTS server (OPSQ0002); the working mock TR is VTRP6504R.
TR_ID_US_PRESENT_BALANCE = "VTRP6504R"

TOKEN_PATH = "/oauth2/tokenP"
HASHKEY_PATH = "/uapi/hashkey"
DAILY_PRICE_PATH = "/uapi/overseas-price/v1/quotations/dailyprice"
ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
PRESENT_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-present-balance"

WATCHLIST = ["NVDA", "PLTR", "AAPL"]

# excd: KIS daily-price EXCD (quotations) | ovrs_excg_cd: KIS order OVRS_EXCG_CD (trading)
# NVDA and PLTR route through NASD on the VTS mock server (NYS fails for PLTR candle fetch).
MARKET_META: dict[str, dict[str, str]] = {
    "NVDA": {"excd": "NAS", "ovrs_excg_cd": "NASD"},   # High-vol market leader (NASDAQ)
    "PLTR": {"excd": "NAS", "ovrs_excg_cd": "NASD"},   # High-momentum breakout (VTS: NASD)
    "AAPL": {"excd": "NAS", "ovrs_excg_cd": "NASD"},   # Low-vol large-cap baseline (NASDAQ)
}

ANALYTICS_SMA_PERIOD = 20

DATA_DIR = "./data"
STATE_FILE = "./trading_state.json"
TOKEN_CACHE_FILE = "./kis_token_cache.json"
LOOKBACK_YEARS = 3
TARGET_BARS = LOOKBACK_YEARS * 252
CAPITAL_AT_RISK = 10_000.0
RISK_PER_TRADE = 0.01
TICKER_SLEEP_SECONDS = 1
LOOP_COOLDOWN_SECONDS = 60

STRATEGY_CONFIG = StrategyConfig(
    sma_period=10,
    rsi_period=14,
    atr_period=14,
    volume_sma_period=20,
    atr_multiplier=2.0,
    rsi_buy_threshold=50.0,
    rsi_upper_limit=70.0,
    volume_threshold=1.2,
    use_trailing_stop=True,
    rsi_exit_mode="crossdown",
    execution_mode="eod",
)


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
        response = requests.post(url, json=body, timeout=15)
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

    def get_hashkey(self, body: dict[str, Any]) -> str:
        url = f"{BASE_URL}{HASHKEY_PATH}"
        token = self.get_access_token()
        response = requests.post(
            url,
            headers={
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": APP_KEY,
                "appsecret": APP_SECRET,
            },
            json=body,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        hashkey = payload.get("HASH")
        if not hashkey:
            raise RuntimeError(f"Hashkey issuance failed: {payload}")
        return hashkey

    def fetch_us_daily_bars(
        self,
        ticker: str,
        excd: str,
        target_bars: int = TARGET_BARS,
    ) -> pd.DataFrame:
        """Fetch US daily OHLCV via TR ID HHDFS76240000 with pagination."""
        url = f"{BASE_URL}{DAILY_PRICE_PATH}"
        collected: list[dict[str, Any]] = []
        bymd = datetime.now().strftime("%Y%m%d")

        while len(collected) < target_bars:
            params = {
                "AUTH": "",
                "EXCD": excd,
                "SYMB": ticker,
                "GUBN": "0",
                "BYMD": bymd,
                "MODP": "1",
            }
            response = requests.get(
                url,
                headers=self._build_headers(TR_ID_DAILY_PRICE),
                params=params,
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()

            if payload.get("rt_cd") not in (None, "0"):
                raise RuntimeError(
                    f"Daily price API error for {ticker}: "
                    f"{payload.get('msg_cd')} | {payload.get('msg1')}"
                )

            batch = payload.get("output2") or []
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
        response = requests.post(
            url,
            headers=self._build_headers(tr_id, include_hashkey=True, hashkey=hashkey),
            json=order_body,
            timeout=15,
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
        url = f"{BASE_URL}{PRESENT_BALANCE_PATH}"
        params = {
            "CANO": CANO,
            "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "WCRC_FRCR_DVSN_CD": wcrc_frcr_dvsn_cd,
            "NATN_CD": natn_cd,
            "TR_MKET_CD": tr_mket_cd,
            "INQR_DVSN_CD": inqr_dvsn_cd,
        }
        response = requests.get(
            url,
            headers=self._build_headers(TR_ID_US_PRESENT_BALANCE),
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("rt_cd") not in (None, "0"):
            raise RuntimeError(
                f"Present balance API error: "
                f"{payload.get('msg_cd')} | {payload.get('msg1')}"
            )

        return payload


def _parse_amount(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)


def _summarize_overseas_present_balance(payload: dict[str, Any]) -> dict[str, float]:
    """Extract deposit and valuation fields from inquire-present-balance output."""
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
    """Print mock account cash/deposit summary before the monitoring loop starts."""
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
    """State that order sizing uses STRATEGY_CONFIG capital, not live broker balances."""
    print("-" * 88)
    print("CAPITAL MODEL - MANUAL (STRATEGY_CONFIG)".center(88))
    print("-" * 88)
    print(f"Capital At Risk      : ${CAPITAL_AT_RISK:,.2f}")
    print(f"Risk Per Trade       : {RISK_PER_TRADE * 100:.1f}%")
    print(f"SMA / RSI / ATR      : {STRATEGY_CONFIG.sma_period} / "
          f"{STRATEGY_CONFIG.rsi_period} / {STRATEGY_CONFIG.atr_period}")
    print(
        "Order Placement      : Not gated on balance inquiry; "
        "mock VTS suffix mismatches are ignored."
    )
    print("-" * 88)


def try_print_mock_account_balance(client: KISApiClient) -> None:
    """
    Best-effort mock balance probe.

    Any failure (network, invalid CANO suffix, VTS rt_cd errors) is swallowed so
    the monitoring loop always starts and orders use STRATEGY_CONFIG capital only.
    """
    try:
        print_mock_account_balance(client)
    except requests.RequestException as exc:
        print(f"[INFO] Balance inquiry ignored (network): {exc}")
    except Exception as exc:
        print(f"[INFO] Balance inquiry ignored: {exc}")


def _parse_kis_daily_output(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Normalize KIS overseas daily price output2 rows into OHLCV DataFrame."""
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


def data_path_for_ticker(ticker: str) -> str:
    return os.path.join(DATA_DIR, f"{ticker.lower()}_daily.csv")


def load_persisted_states(path: str = STATE_FILE) -> dict[str, Any]:
    """
    Load multi-ticker persistence state.

    Expected structure:
      {"NVDA": {...}, "PLTR": {...}, "AAPL": {...}}
    """
    if not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError("Invalid trading state file: root must be a dictionary.")
    return payload


def save_persisted_states(states: dict[str, Any], path: str = STATE_FILE) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(states, handle, indent=2, sort_keys=True)


def persist_market_data(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)


def fetch_us_stock_candles(
    client: KISApiClient,
    ticker: str,
) -> pd.DataFrame:
    """Fetch and normalize US daily OHLCV candles for a watchlist ticker."""
    if ticker not in MARKET_META:
        raise ValueError(f"Ticker {ticker} is not configured in MARKET_META.")

    excd = MARKET_META[ticker]["excd"]
    df = client.fetch_us_daily_bars(ticker=ticker, excd=excd)
    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    df = df[(df["Close"] > 0) & (df["Volume"] >= 0)].reset_index(drop=True)

    if len(df) < 22:
        raise ValueError(f"Insufficient bar count for {ticker}: {len(df)}")

    return df


def execute_broker_order(
    client: KISApiClient,
    ticker: str,
    signal: str,
    size: int,
) -> dict[str, Any] | None:
    """Route actionable signals to the KIS US equity order endpoint."""
    ovrs_excg_cd = MARKET_META[ticker]["ovrs_excg_cd"]

    if signal == "BUY" and size > 0:
        print(
            f"[KIS ORDER] BUY  {ticker} | qty={size} | market order | "
            f"tr_id={TR_ID_US_BUY}"
        )
        return client.place_us_order(
            side="BUY",
            ticker=ticker,
            ovrs_excg_cd=ovrs_excg_cd,
            quantity=size,
            price=0.0,
        )

    if signal in {"SELL", "DYNAMIC_ATR_SELL"}:
        print(
            f"[KIS ORDER] SELL {ticker} | close-position | market order | "
            f"tr_id={TR_ID_US_SELL}"
        )
        return client.place_us_order(
            side="SELL",
            ticker=ticker,
            ovrs_excg_cd=ovrs_excg_cd,
            quantity=max(size, 1),
            price=0.0,
        )

    print(f"[KIS ORDER] NO ACTION | {ticker} | signal={signal}")
    return None


def _analytics_sma20(df: pd.DataFrame) -> float:
    if len(df) < ANALYTICS_SMA_PERIOD:
        return float("nan")
    return float(df["Close"].rolling(window=ANALYTICS_SMA_PERIOD).mean().iloc[-1])


def print_session_telemetry(
    ticker: str,
    session: dict[str, Any],
    df: pd.DataFrame,
) -> None:
    """Emit pipe-friendly metrics for empirical log collection and visualization."""
    metrics = session["metrics"]["today"]
    signal = session["signal_result"]["signal"]
    close = float(metrics["close"])
    sma_20 = _analytics_sma20(df)
    rsi = float(metrics["rsi"])
    atr = float(metrics["atr"])
    volume = float(metrics["volume"])
    volume_avg_20 = float(metrics["volume_sma"])
    volume_ratio = volume / volume_avg_20 if volume_avg_20 > 0 else 0.0
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("-" * 88)
    print(
        f"[METRICS] {ticker} | {timestamp} | "
        f"Close={close:.2f} | SMA20={sma_20:.2f} | RSI={rsi:.2f} | "
        f"ATR_Wilder={atr:.4f} | Volume={volume:,.0f}/{volume_avg_20:,.0f} "
        f"({volume_ratio:.3f}x)"
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
    print(f"Signal               : {signal}")
    print(f"Position Size (shares): {session['position_size']}")
    print(f"Liquidity OK         : {session['signal_result'].get('liquidity_ok', 'N/A')}")

    if signal == "DYNAMIC_ATR_SELL":
        stop = session["signal_result"]["dynamic_atr_stop"]
        print(
            f"ATR Stop Telemetry   : peak=${stop['captured_peak']:.2f} | "
            f"floor=${stop['trigger_floor']:.2f} | "
            f"exec=${stop['current_execution_price']:.2f}"
        )


def process_ticker(
    client: KISApiClient,
    engine: LiveSignalEngine,
    ticker: str,
    states: dict[str, Any],
) -> str:
    """Run the full fetch-evaluate-execute-persist cycle for one ticker."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Processing ticker: {ticker}")

    df = fetch_us_stock_candles(client, ticker)
    persist_market_data(df, data_path_for_ticker(ticker))
    print(f"  -> {len(df)} bars cached to {data_path_for_ticker(ticker)}")

    ticker_state = states.get(ticker)
    session = engine.evaluate_session(
        df,
        external_state=ticker_state,
        capital_at_risk=CAPITAL_AT_RISK,
        risk_per_trade=RISK_PER_TRADE,
    )

    print_session_telemetry(ticker, session, df)

    signal = session["signal_result"]["signal"]
    size = int(session["position_size"])

    if signal in {"BUY", "SELL", "DYNAMIC_ATR_SELL"}:
        execute_broker_order(client, ticker, signal, size)

    enriched = engine.enrich(df)
    post_session_state = engine.replay_state(
        enriched,
        end_index=len(enriched) - 1,
        initial_state=ticker_state,
    )
    states[ticker] = engine.dump_state(post_session_state)
    save_persisted_states(states)

    print(f"  -> State persisted for {ticker}")
    return signal


def run_watchlist_cycle(
    client: KISApiClient,
    engine: LiveSignalEngine,
    states: dict[str, Any],
) -> dict[str, Any]:
    """Execute one full pass across the watchlist."""
    summary: list[tuple[str, str]] = []

    for index, ticker in enumerate(WATCHLIST):
        try:
            signal = process_ticker(client, engine, ticker, states)
            summary.append((ticker, signal))
        except requests.Timeout as exc:
            print(f"[ERROR] Timeout for {ticker}: {exc}")
            summary.append((ticker, "TIMEOUT"))
        except requests.RequestException as exc:
            print(f"[ERROR] Network failure for {ticker}: {exc}")
            summary.append((ticker, "NETWORK_ERROR"))
        except Exception as exc:
            print(f"[ERROR] Unhandled failure for {ticker}: {exc}")
            traceback.print_exc()
            summary.append((ticker, "FAILED"))

        if index < len(WATCHLIST) - 1:
            time.sleep(TICKER_SLEEP_SECONDS)

    print()
    print("=" * 88)
    print("WATCHLIST CYCLE SUMMARY".center(88))
    print("=" * 88)
    for ticker, status in summary:
        print(f"  {ticker:<6} -> {status}")
    print("=" * 88)

    return states


def main() -> None:
    validate_environment()

    print("KIS Mock Trading - Continuous Market Monitor")
    print(f"Execution Timestamp : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Base URL            : {BASE_URL}")
    print(f"Account             : {CANO}-{ACNT_PRDT_CD}")
    print(f"Watchlist           : {', '.join(WATCHLIST)}")
    print(f"State File          : {STATE_FILE}")
    print(f"Ticker Interval     : {TICKER_SLEEP_SECONDS}s")
    print(f"Loop Cooldown       : {LOOP_COOLDOWN_SECONDS}s")
    print("=" * 88)

    client = KISApiClient()
    engine = LiveSignalEngine(STRATEGY_CONFIG)

    print("Requesting KIS access token...")
    client.get_access_token()
    print("Access token ready.")

    states = load_persisted_states()
    print(f"Loaded state entries: {len(states)}")

    try_print_mock_account_balance(client)
    log_configured_capital_model()

    print("Starting continuous monitoring loop. Press Ctrl+C to stop.")
    print("Order execution is NOT blocked by balance inquiry status.")
    print("=" * 88)

    cycle_count = 0

    try:
        while True:
            cycle_count += 1
            cycle_started = datetime.now()
            print()
            print(f"--- Cycle {cycle_count} started at {cycle_started.strftime('%Y-%m-%d %H:%M:%S')} ---")

            states = run_watchlist_cycle(client, engine, states)

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


if __name__ == "__main__":
    main()
