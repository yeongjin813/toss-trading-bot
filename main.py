"""
Production execution loop — Korea Investment & Securities (KIS) mock trading API.

Setup:
  Create a `.env` file in the project root with the following keys:

    KIS_APP_KEY=your_app_key
    KIS_APP_SECRET=your_app_secret
    KIS_CANO=your_account_number
    KIS_ACNT_PRDT_CD=01
    WATCHLIST=NVDA,PLTR,AAPL
    CAPITAL_AT_RISK=10000

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

from analytics import (
    LiveSignalEngine,
    PositionState,
    StrategyConfig,
    describe_us_market_closure,
    is_us_equity_session,
    is_us_regular_market_hours,
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

TOKEN_PATH = "/oauth2/tokenP"
HASHKEY_PATH = "/uapi/hashkey"
DAILY_PRICE_PATH = "/uapi/overseas-price/v1/quotations/dailyprice"
ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
PRESENT_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-present-balance"

DEFAULT_WATCHLIST = ["NVDA", "PLTR", "AAPL"]


def _parse_watchlist(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_WATCHLIST.copy()
    tickers = [token.strip().upper() for token in raw.split(",") if token.strip()]
    return tickers or DEFAULT_WATCHLIST.copy()


WATCHLIST = _parse_watchlist(os.getenv("WATCHLIST"))

MARKET_META: dict[str, dict[str, str]] = {
    "NVDA": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "PLTR": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "AAPL": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
}

ANALYTICS_SMA_PERIOD = 20
MIN_DATA_BARS = 22

DATA_DIR = "./data"
STATE_FILE = "./trading_state.json"
TOKEN_CACHE_FILE = "./kis_token_cache.json"
LOOKBACK_YEARS = 3
TARGET_BARS = LOOKBACK_YEARS * 252
CAPITAL_AT_RISK = float(os.getenv("CAPITAL_AT_RISK", "10000"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
TICKER_SLEEP_SECONDS = int(os.getenv("TICKER_SLEEP_SECONDS", "1"))
LOOP_COOLDOWN_SECONDS = int(os.getenv("LOOP_COOLDOWN_SECONDS", "60"))
MARKET_CLOSED_SLEEP_SECONDS = int(os.getenv("MARKET_CLOSED_SLEEP_SECONDS", "3600"))

STRATEGY_CONFIG = StrategyConfig.from_env()


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

    unknown = [ticker for ticker in WATCHLIST if ticker not in MARKET_META]
    if unknown:
        raise RuntimeError(
            f"Watchlist tickers missing MARKET_META routing: {', '.join(unknown)}"
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

    def _fetch_daily_price_batch(
        self,
        ticker: str,
        excd: str,
        bymd: str,
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

        return payload.get("output2") or []

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
            batch = self._fetch_daily_price_batch(ticker, excd, bymd)
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


class MarketDataCache:
    """
    Bootstrap full historical windows once, then refresh only the latest bar.

    Avoids re-downloading ~756 bars every 60-second polling cycle.
    """

    def __init__(self, client: KISApiClient) -> None:
        self._client = client
        self._frames: dict[str, pd.DataFrame] = {}

    def bootstrap(self, tickers: list[str]) -> None:
        for ticker in tickers:
            self._frames[ticker] = self._load_or_fetch_full_history(ticker)
            persist_market_data(self._frames[ticker], data_path_for_ticker(ticker))
            print(
                f"  -> Bootstrap {ticker}: {len(self._frames[ticker])} bars cached "
                f"to {data_path_for_ticker(ticker)}"
            )

    def get_frame(self, ticker: str) -> pd.DataFrame:
        if ticker not in self._frames:
            raise KeyError(f"Ticker {ticker} is not bootstrapped in MarketDataCache.")
        return self._frames[ticker].copy()

    def refresh_latest(self, ticker: str) -> tuple[pd.DataFrame, bool]:
        """
        Pull the latest KIS daily page and merge into the cached frame.

        Returns (updated_frame, is_new_bar).
        """
        excd = MARKET_META[ticker]["excd"]
        latest_batch = self._client.fetch_us_daily_latest(ticker, excd)
        cached = self._frames.get(ticker)

        if cached is None or cached.empty:
            cached = self._load_or_fetch_full_history(ticker)
            self._frames[ticker] = cached
            return cached, True

        if latest_batch.empty:
            return cached, False

        latest_batch = normalize_market_frame(latest_batch)
        latest_row = latest_batch.sort_values("Date").iloc[-1]
        latest_date = latest_row["Date"]

        if latest_date not in set(cached["Date"]):
            merged = pd.concat([cached, latest_row.to_frame().T], ignore_index=True)
            merged = (
                merged.drop_duplicates(subset=["Date"])
                .sort_values("Date")
                .tail(TARGET_BARS)
                .reset_index(drop=True)
            )
            self._frames[ticker] = merged
            return merged, True

        idx = cached[cached["Date"] == latest_date].index[-1]
        cached.loc[idx, ["Open", "High", "Low", "Close", "Volume"]] = latest_row[
            ["Open", "High", "Low", "Close", "Volume"]
        ].values
        self._frames[ticker] = cached.reset_index(drop=True)
        return self._frames[ticker], False

    def _load_or_fetch_full_history(self, ticker: str) -> pd.DataFrame:
        path = data_path_for_ticker(ticker)
        if os.path.exists(path):
            cached = pd.read_csv(path)
            cached = normalize_market_frame(cached)
            if len(cached) >= MIN_DATA_BARS:
                return cached

        excd = MARKET_META[ticker]["excd"]
        df = self._client.fetch_us_daily_bars(ticker=ticker, excd=excd)
        return normalize_market_frame(df)


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


def normalize_market_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).copy()
    frame = frame[(frame["Close"] > 0) & (frame["Volume"] >= 0)]
    frame = frame.drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)
    if len(frame) < MIN_DATA_BARS:
        raise ValueError(f"Insufficient bar count after normalization: {len(frame)}")
    return frame


def data_path_for_ticker(ticker: str) -> str:
    return os.path.join(DATA_DIR, f"{ticker.lower()}_daily.csv")


def load_persisted_states(path: str = STATE_FILE) -> dict[str, Any]:
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


def _resolve_execution_quantity(
    signal: str,
    proposed_size: int,
    runtime: PositionState,
) -> int:
    if signal == "BUY":
        return max(int(proposed_size), 0)

    if signal in {"SELL", "DYNAMIC_ATR_SELL"}:
        held = max(int(runtime.held_quantity), 0)
        if held <= 0 and runtime.has_position:
            held = max(int(proposed_size), 1)
        return held

    return 0


def execute_broker_order(
    client: KISApiClient,
    ticker: str,
    signal: str,
    quantity: int,
) -> dict[str, Any]:
    ovrs_excg_cd = MARKET_META[ticker]["ovrs_excg_cd"]

    if signal == "BUY" and quantity > 0:
        print(
            f"[KIS ORDER] BUY  {ticker} | qty={quantity} | market order | "
            f"tr_id={TR_ID_US_BUY}"
        )
        return client.place_us_order(
            side="BUY",
            ticker=ticker,
            ovrs_excg_cd=ovrs_excg_cd,
            quantity=quantity,
            price=0.0,
        )

    if signal in {"SELL", "DYNAMIC_ATR_SELL"} and quantity > 0:
        print(
            f"[KIS ORDER] SELL {ticker} | qty={quantity} | close-position | "
            f"market order | tr_id={TR_ID_US_SELL}"
        )
        return client.place_us_order(
            side="SELL",
            ticker=ticker,
            ovrs_excg_cd=ovrs_excg_cd,
            quantity=quantity,
            price=0.0,
        )

    print(f"[KIS ORDER] NO ACTION | {ticker} | signal={signal} | qty={quantity}")
    return {"rt_cd": "0", "skipped": True}


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
) -> str:
    """
    Enforce order lifecycle:
      pending lock -> KIS dispatch -> rt_cd verify -> transition -> persist
    """
    if runtime.pending_order:
        print(f"[LOCK] {ticker} pending_order=True — signal suppressed")
        return "LOCKED"

    execution_qty = _resolve_execution_quantity(signal, proposed_size, runtime)
    if signal in {"SELL", "DYNAMIC_ATR_SELL"} and execution_qty <= 0:
        print(f"[SKIP] {ticker} liquidation skipped — no held quantity tracked")
        return "HOLD"

    if signal == "BUY" and execution_qty <= 0:
        print(f"[SKIP] {ticker} BUY skipped — zero share size")
        return "HOLD"

    if signal not in {"BUY", "SELL", "DYNAMIC_ATR_SELL"}:
        return signal

    order_payload = execute_broker_order(client, ticker, signal, execution_qty)
    if order_payload.get("skipped"):
        return signal

    if str(order_payload.get("rt_cd", "")) != "0":
        raise RuntimeError(
            f"Order transmission failed for {ticker}: rt_cd={order_payload.get('rt_cd')}"
        )

    runtime.pending_order = True
    states[ticker] = engine.dump_state(runtime)
    save_persisted_states(states)

    engine.apply_post_order_transition(
        runtime,
        signal=signal,
        filled_quantity=execution_qty,
        current_bar_date=current_bar_date,
        allow_crossover=allow_crossover,
    )

    states[ticker] = engine.dump_state(runtime)
    save_persisted_states(states)

    print(
        f"  -> Order accepted (rt_cd=0). State transition applied for {ticker}: "
        f"has_position={runtime.has_position}, held_qty={runtime.held_quantity}"
    )
    return signal


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
    print(f"Bar Date             : {cycle['current_bar_date']}")
    print(f"Session Low          : {cycle['session_low']:.4f}")
    print(f"Calendar Open        : {cycle.get('calendar_open', cycle.get('market_open'))}")
    print(f"Regular Market Hours : {cycle.get('regular_market_hours', cycle.get('market_open'))}")
    print(f"Crossover Allowed    : {cycle['allow_crossover']}")
    print(f"Signal               : {signal}")
    print(f"Position Size (shares): {cycle['position_size']}")
    print(f"Liquidity OK         : {cycle['signal_result'].get('liquidity_ok', 'N/A')}")
    runtime = cycle["runtime_state"]
    print(
        f"Runtime Registry     : pending={runtime.get('pending_order')} | "
        f"held_qty={runtime.get('held_quantity')} | "
        f"last_processed={runtime.get('last_processed_date')}"
    )

    if signal == "DYNAMIC_ATR_SELL":
        stop = cycle["signal_result"].get("dynamic_atr_stop") or {}
        print(
            f"ATR Stop Telemetry   : peak=${stop.get('captured_peak', 0):.2f} | "
            f"floor=${stop.get('trigger_floor', 0):.2f} | "
            f"session_low=${stop.get('session_low', cycle['session_low']):.2f}"
        )


def process_ticker(
    client: KISApiClient,
    engine: LiveSignalEngine,
    cache: MarketDataCache,
    ticker: str,
    states: dict[str, Any],
) -> str:
    """Run fetch-evaluate-execute-persist cycle for one ticker with state machine gates."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Processing ticker: {ticker}")

    df, is_new_bar = cache.refresh_latest(ticker)
    persist_market_data(df, data_path_for_ticker(ticker))
    print(
        f"  -> {len(df)} bars in memory "
        f"({'new daily bar' if is_new_bar else 'intraday refresh'})"
    )

    ticker_state = states.get(ticker, {})
    cycle = engine.evaluate_trading_cycle(
        df,
        runtime_state=ticker_state,
        capital_at_risk=CAPITAL_AT_RISK,
        risk_per_trade=RISK_PER_TRADE,
    )

    print_session_telemetry(ticker, cycle, df)

    runtime = engine.load_state(cycle["runtime_state"])
    signal = cycle["signal_result"]["signal"]
    proposed_size = int(cycle["position_size"])

    if cycle["signal_result"].get("pending_order_locked"):
        print(f"[LOCK] {ticker} pending order lock active — no dispatch")
        states[ticker] = engine.dump_state(runtime)
        save_persisted_states(states)
        return "LOCKED"

    if signal in {"BUY", "SELL"} and not cycle.get("regular_market_hours", False):
        print(
            f"[GATE] {ticker} outside NY regular session (09:30–16:00 ET) — "
            f"{signal} blocked, holding"
        )
        states[ticker] = engine.dump_state(runtime)
        save_persisted_states(states)
        return "HOLD"

    if signal in {"BUY", "SELL", "DYNAMIC_ATR_SELL"}:
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
        )
    else:
        final_signal = signal
        states[ticker] = engine.dump_state(runtime)
        save_persisted_states(states)

    print(f"  -> State persisted for {ticker}")
    return final_signal


def run_watchlist_cycle(
    client: KISApiClient,
    engine: LiveSignalEngine,
    cache: MarketDataCache,
    states: dict[str, Any],
) -> dict[str, Any]:
    summary: list[tuple[str, str]] = []

    if not is_us_equity_session():
        closure_reason = describe_us_market_closure() or "market closed"
        print(
            f"[GATE] US calendar session closed ({closure_reason}) — "
            "crossover suppressed; ATR stop still active for open positions"
        )
    elif not is_us_regular_market_hours():
        print(
            "[GATE] Outside NY regular hours (09:30–16:00 ET) — "
            "crossover suppressed; ATR stop active for open positions"
        )

    for index, ticker in enumerate(WATCHLIST):
        try:
            signal = process_ticker(client, engine, cache, ticker, states)
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

    print("KIS Mock Trading - Production Execution Engine")
    print(f"Execution Timestamp : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Base URL            : {BASE_URL}")
    print(f"Account             : {CANO}-{ACNT_PRDT_CD}")
    print(f"Watchlist           : {', '.join(WATCHLIST)}")
    print(f"Capital At Risk     : ${CAPITAL_AT_RISK:,.2f}")
    print(f"State File          : {STATE_FILE}")
    print(f"Ticker Interval     : {TICKER_SLEEP_SECONDS}s")
    print(f"Loop Cooldown       : {LOOP_COOLDOWN_SECONDS}s")
    print(f"Market Closed Sleep : {MARKET_CLOSED_SLEEP_SECONDS}s")
    print("=" * 88)

    client = KISApiClient()
    engine = LiveSignalEngine(STRATEGY_CONFIG)
    cache = MarketDataCache(client)

    print("Requesting KIS access token...")
    client.get_access_token()
    print("Access token ready.")

    print("Bootstrapping historical market data cache (one-time full fetch)...")
    cache.bootstrap(WATCHLIST)

    states = load_persisted_states()
    print(f"Loaded state entries: {len(states)}")

    try_print_mock_account_balance(client)
    log_configured_capital_model()

    print("Starting production monitoring loop. Press Ctrl+C to stop.")
    print("Order pipeline: KIS dispatch -> rt_cd verify -> state transition -> persist")
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

            print()
            print(
                f"--- Cycle {cycle_count} started at "
                f"{cycle_started.strftime('%Y-%m-%d %H:%M:%S')} ---"
            )

            states = run_watchlist_cycle(client, engine, cache, states)

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
