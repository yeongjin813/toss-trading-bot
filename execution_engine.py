"""
Live execution hardening: fill verification, trade logging, and risk gates.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

FillCallback = Callable[[str, str, int, float, str], None]
AlertCallback = Callable[[str, str], None]

from analytics import PositionState, _to_ny_datetime
from market_registry import MARKET_META, sector_for_ticker

TRADE_LOG_COLUMNS = (
    "timestamp",
    "ticker",
    "signal",
    "qty",
    "order_price",
    "fill_price",
    "status",
    "reason",
    "cash_after",
    "held_qty",
)

HIGH_VOL_TICKERS = frozenset({"NVDA", "TSLA", "PLTR", "CRWD", "AMD", "META"})


@dataclass
class ExecutionSettings:
    max_daily_loss_usd: float
    max_open_positions: int
    max_ticker_exposure_usd: float
    max_portfolio_usd: float
    rth_buy_block_open_minutes: int
    rth_buy_block_close_minutes: int
    pending_order_stale_minutes: int
    pending_order_cancel_minutes: int
    fill_inquiry_alert_cooldown_minutes: int
    default_limit_buffer_bps: float
    high_vol_limit_buffer_bps: float
    max_consecutive_loss_days: int
    max_positions_per_sector: int

    @classmethod
    def from_env(cls) -> ExecutionSettings:
        return cls(
            max_daily_loss_usd=float(os.getenv("MAX_DAILY_LOSS_USD", "100")),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "3")),
            max_ticker_exposure_usd=float(os.getenv("MAX_TICKER_EXPOSURE_USD", "1000")),
            max_portfolio_usd=float(
                os.getenv(
                    "MAX_PORTFOLIO_USD",
                    os.getenv("CAPITAL_AT_RISK", "10000"),
                )
            ),
            rth_buy_block_open_minutes=int(os.getenv("RTH_BUY_BLOCK_OPEN_MINUTES", "10")),
            rth_buy_block_close_minutes=int(os.getenv("RTH_BUY_BLOCK_CLOSE_MINUTES", "5")),
            pending_order_stale_minutes=int(
                os.getenv("PENDING_ORDER_STALE_MINUTES", "120")
            ),
            pending_order_cancel_minutes=int(
                os.getenv("PENDING_ORDER_CANCEL_MINUTES", "45")
            ),
            fill_inquiry_alert_cooldown_minutes=int(
                os.getenv("FILL_INQUIRY_ALERT_COOLDOWN_MINUTES", "15")
            ),
            default_limit_buffer_bps=float(os.getenv("KIS_LIMIT_PRICE_BUFFER_BPS", "10")),
            high_vol_limit_buffer_bps=float(
                os.getenv("KIS_HIGH_VOL_LIMIT_BUFFER_BPS", "15")
            ),
            max_consecutive_loss_days=int(os.getenv("MAX_CONSECUTIVE_LOSS_DAYS", "3")),
            max_positions_per_sector=int(os.getenv("MAX_POSITIONS_PER_SECTOR", "2")),
        )


def limit_buffer_bps_for_ticker(ticker: str, settings: ExecutionSettings) -> float:
    if ticker.upper() in HIGH_VOL_TICKERS:
        return settings.high_vol_limit_buffer_bps
    return settings.default_limit_buffer_bps


def limit_order_price(
    side: str, reference_price: float, buffer_bps: float
) -> float:
    if reference_price <= 0:
        raise ValueError("Reference price must be positive for limit orders.")
    buffer = buffer_bps / 10_000.0
    if side.upper() == "BUY":
        return round(reference_price * (1.0 + buffer), 2)
    return round(reference_price * (1.0 - buffer), 2)


def extract_order_odno(payload: dict[str, Any]) -> str | None:
    output = payload.get("output") or {}
    if isinstance(output, list):
        output = output[0] if output else {}
    if not isinstance(output, dict):
        return None
    for key in ("ODNO", "odno", "ORD_NO", "ord_no"):
        raw = output.get(key)
        if raw not in (None, ""):
            return str(raw).strip()
    return None


def clear_open_order(runtime: PositionState) -> None:
    runtime.pending_order = False
    runtime.open_order_id = None
    runtime.open_order_side = None
    runtime.open_order_qty = 0
    runtime.open_order_price = None
    runtime.open_order_submitted_at = None
    runtime.open_order_filled_qty = 0


def assign_open_order(
    runtime: PositionState,
    *,
    odno: str,
    side: str,
    qty: int,
    price: float,
    submitted_at: str,
) -> None:
    runtime.pending_order = True
    runtime.open_order_id = odno
    runtime.open_order_side = side
    runtime.open_order_qty = qty
    runtime.open_order_price = price
    runtime.open_order_submitted_at = submitted_at
    runtime.open_order_filled_qty = 0


def ny_today_yyyymmdd(now: datetime | None = None) -> str:
    return _to_ny_datetime(now).strftime("%Y%m%d")


def block_new_buy_rth_window(now: datetime | None, settings: ExecutionSettings) -> str | None:
    """Return block reason for new BUY during open/close volatility windows."""
    ny_dt = _to_ny_datetime(now)
    if ny_dt.weekday() >= 5:
        return None

    open_minutes = (
        ny_dt.hour * 60 + ny_dt.minute
    ) - (9 * 60 + 30)
    if 0 <= open_minutes < settings.rth_buy_block_open_minutes:
        return (
            f"RTH open window (first {settings.rth_buy_block_open_minutes}m after 09:30 ET)"
        )

    close_minutes = (16 * 60) - (ny_dt.hour * 60 + ny_dt.minute)
    if 0 < close_minutes <= settings.rth_buy_block_close_minutes:
        return (
            f"RTH close window (last {settings.rth_buy_block_close_minutes}m before 16:00 ET)"
        )
    return None


class TradeLogWriter:
    def __init__(self, path: str = "./trade_log.csv") -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(TRADE_LOG_COLUMNS)

    def append(
        self,
        *,
        ticker: str,
        signal: str,
        qty: int,
        order_price: float | None,
        fill_price: float | None,
        status: str,
        reason: str,
        cash_after: float | None,
        held_qty: int,
    ) -> None:
        row = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ticker": ticker,
            "signal": signal,
            "qty": qty,
            "order_price": "" if order_price is None else f"{order_price:.2f}",
            "fill_price": "" if fill_price is None else f"{fill_price:.2f}",
            "status": status,
            "reason": reason,
            "cash_after": "" if cash_after is None else f"{cash_after:.2f}",
            "held_qty": held_qty,
        }
        with open(self.path, "a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([row[column] for column in TRADE_LOG_COLUMNS])


class RiskGuard:
    def __init__(self, settings: ExecutionSettings) -> None:
        self.settings = settings

    def _portfolio_block(self, states: dict[str, Any], now: datetime | None) -> str | None:
        portfolio = states.get("_portfolio", {})
        ny_date = _to_ny_datetime(now).strftime("%Y-%m-%d")
        anchor_date = portfolio.get("daily_pnl_anchor_date")
        day_start = float(portfolio.get("day_start_equity_usd", 0.0) or 0.0)
        current = float(portfolio.get("last_equity_usd", day_start) or day_start)

        if anchor_date != ny_date or day_start <= 0:
            return None

        loss = day_start - current
        if loss >= self.settings.max_daily_loss_usd:
            return (
                f"daily loss limit (${loss:.2f} >= "
                f"${self.settings.max_daily_loss_usd:.2f})"
            )

        consecutive = int(portfolio.get("consecutive_loss_days", 0) or 0)
        if (
            self.settings.max_consecutive_loss_days > 0
            and consecutive >= self.settings.max_consecutive_loss_days
        ):
            return (
                f"consecutive loss days ({consecutive} >= "
                f"{self.settings.max_consecutive_loss_days})"
            )
        return None

    def _effective_max_open_positions(
        self,
        *,
        max_open_positions_override: int | None = None,
    ) -> int:
        if max_open_positions_override is not None:
            return max(0, max_open_positions_override)
        return self.settings.max_open_positions

    def _sector_position_count(
        self,
        states: dict[str, Any],
        sector: str,
        *,
        exclude_ticker: str | None = None,
    ) -> int:
        count = 0
        for key, payload in states.items():
            if str(key).startswith("_") or not isinstance(payload, dict):
                continue
            if exclude_ticker and str(key).upper() == exclude_ticker.upper():
                continue
            if int(payload.get("held_quantity", 0) or 0) <= 0:
                continue
            if sector_for_ticker(str(key)) == sector:
                count += 1
        return count

    def check_buy_allowed(
        self,
        ticker: str,
        proposed_size: int,
        entry_price: float,
        states: dict[str, Any],
        *,
        now: datetime | None = None,
        deployable_cash_usd: float | None = None,
        portfolio_deployed_usd: float = 0.0,
        max_open_positions_override: int | None = None,
    ) -> str | None:
        block = self._portfolio_block(states, now)
        if block:
            return block

        if deployable_cash_usd is not None and deployable_cash_usd <= 0:
            return "no deployable cash (portfolio at or above capital cap)"

        max_positions = self._effective_max_open_positions(
            max_open_positions_override=max_open_positions_override,
        )
        open_positions = sum(
            1
            for key, payload in states.items()
            if not str(key).startswith("_")
            and isinstance(payload, dict)
            and int(payload.get("held_quantity", 0) or 0) > 0
        )
        ticker_state = states.get(ticker, {})
        already_held = int(ticker_state.get("held_quantity", 0) or 0) > 0
        if not already_held and open_positions >= max_positions:
            return (
                f"max open positions ({open_positions} >= "
                f"{max_positions})"
            )

        sector = sector_for_ticker(ticker)
        if (
            self.settings.max_positions_per_sector > 0
            and sector not in {"benchmark", "other"}
            and not already_held
        ):
            sector_count = self._sector_position_count(
                states,
                sector,
                exclude_ticker=ticker,
            )
            if sector_count >= self.settings.max_positions_per_sector:
                return (
                    f"sector {sector} concentration "
                    f"({sector_count} >= {self.settings.max_positions_per_sector})"
                )

        notional = proposed_size * entry_price
        held_qty = int(ticker_state.get("held_quantity", 0) or 0)
        existing_notional = held_qty * entry_price
        if existing_notional + notional > self.settings.max_ticker_exposure_usd:
            return (
                f"max ticker exposure (${existing_notional + notional:.2f} > "
                f"${self.settings.max_ticker_exposure_usd:.2f})"
            )

        if portfolio_deployed_usd + notional > self.settings.max_portfolio_usd:
            return (
                f"portfolio cap (${portfolio_deployed_usd + notional:.2f} > "
                f"${self.settings.max_portfolio_usd:.2f})"
            )

        if (
            deployable_cash_usd is not None
            and notional > deployable_cash_usd * 1.01
        ):
            return (
                f"insufficient deployable cash (${notional:.2f} > "
                f"${deployable_cash_usd:.2f})"
            )
        return None

    def update_daily_equity_anchor(
        self,
        states: dict[str, Any],
        portfolio_equity: float,
        *,
        now: datetime | None = None,
    ) -> None:
        portfolio = states.setdefault("_portfolio", {})
        ny_date = _to_ny_datetime(now).strftime("%Y-%m-%d")
        prev_date = portfolio.get("daily_pnl_anchor_date")
        prev_start = float(portfolio.get("day_start_equity_usd", 0.0) or 0.0)
        prev_close = float(portfolio.get("last_equity_usd", prev_start) or prev_start)

        if prev_date and prev_date != ny_date and prev_start > 0:
            if prev_close < prev_start:
                portfolio["consecutive_loss_days"] = int(
                    portfolio.get("consecutive_loss_days", 0) or 0
                ) + 1
            else:
                portfolio["consecutive_loss_days"] = 0

        if portfolio.get("daily_pnl_anchor_date") != ny_date:
            portfolio["daily_pnl_anchor_date"] = ny_date
            portfolio["day_start_equity_usd"] = portfolio_equity
        portfolio["last_equity_usd"] = portfolio_equity


def _parse_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _parse_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def summarize_ccnl_fills(
    rows: list[dict[str, Any]],
    *,
    odno: str,
    ticker: str,
) -> tuple[int, float]:
    """Return (filled_qty, vwap_fill_price) for a specific order."""
    filled_qty = 0
    weighted = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_odno = str(row.get("odno") or row.get("ODNO") or "").strip()
        row_ticker = str(row.get("pdno") or row.get("PDNO") or "").strip().upper()
        if row_odno != odno or row_ticker != ticker.upper():
            continue
        qty = _parse_int(row.get("ft_ccld_qty") or row.get("FT_CCLD_QTY"))
        price = _parse_float(row.get("ft_ccld_unpr3") or row.get("FT_CCLD_UNPR3"))
        if qty <= 0:
            continue
        filled_qty += qty
        weighted += qty * price
    avg_price = weighted / filled_qty if filled_qty > 0 else 0.0
    return filled_qty, avg_price


def order_still_open(
    rows: list[dict[str, Any]],
    *,
    odno: str,
    ticker: str,
) -> bool:
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_odno = str(row.get("odno") or row.get("ODNO") or "").strip()
        row_ticker = str(row.get("pdno") or row.get("PDNO") or "").strip().upper()
        if row_odno != odno or row_ticker != ticker.upper():
            continue
        nccs = _parse_int(row.get("nccs_qty") or row.get("NCCS_QTY"))
        if nccs > 0:
            return True
    return False


def pending_order_age_minutes(runtime: PositionState, now: datetime | None = None) -> float:
    submitted = runtime.open_order_submitted_at
    if not submitted:
        return 0.0
    try:
        submitted_at = datetime.fromisoformat(submitted)
    except ValueError:
        return 0.0
    current = now or datetime.now()
    return max((current - submitted_at).total_seconds() / 60.0, 0.0)


class OrderFillMonitor:
    """Poll KIS ccnl/nccs and broker holdings before mutating local position state."""

    def __init__(
        self,
        settings: ExecutionSettings,
        trade_log: TradeLogWriter,
        *,
        on_fill: FillCallback | None = None,
        on_alert: AlertCallback | None = None,
    ) -> None:
        self.settings = settings
        self.trade_log = trade_log
        self.on_fill = on_fill
        self.on_alert = on_alert
        self._fill_inquiry_alert_at: dict[str, datetime] = {}

    def _notify_fill(
        self,
        ticker: str,
        side: str,
        quantity: int,
        fill_price: float,
        status: str,
    ) -> None:
        if quantity <= 0 or not self.on_fill:
            return
        try:
            self.on_fill(ticker, side, quantity, fill_price, status)
        except Exception as exc:
            print(f"[NOTIFY/FILL] callback failed for {ticker}: {exc}")

    def _notify_alert(self, level: str, message: str) -> None:
        if not self.on_alert:
            return
        try:
            self.on_alert(level, message)
        except Exception as exc:
            print(f"[NOTIFY/ALERT] callback failed: {exc}")

    def _notify_fill_inquiry_failure(self, ticker: str, exc: BaseException) -> None:
        now = datetime.now()
        last_alert = self._fill_inquiry_alert_at.get(ticker)
        cooldown = timedelta(minutes=self.settings.fill_inquiry_alert_cooldown_minutes)
        if last_alert is not None and now - last_alert < cooldown:
            print(
                f"[FILL/POLL] {ticker} inquiry failed (alert suppressed): {exc}"
            )
            return

        self._fill_inquiry_alert_at[ticker] = now
        print(f"[FILL/POLL] {ticker} inquiry failed: {exc}")
        self._notify_alert(
            "WARNING",
            f"{ticker} KIS fill inquiry failed: {exc}",
        )

    def _fetch_live_broker_qty(
        self,
        client: Any,
        ticker: str,
        states: dict[str, Any],
    ) -> int:
        """Refresh one ticker's held qty via present-balance when ccnl/nccs are down."""
        cached = int(
            states.get("_portfolio", {})
            .get("broker_holdings", {})
            .get(ticker, 0)
            or 0
        )
        try:
            from session_manager import resolve_broker_holdings

            payload = client.fetch_overseas_present_balance(natn_cd="840")
            holdings = resolve_broker_holdings(client, [ticker], payload)
            qty = int(holdings.get(ticker, 0))
            portfolio = states.setdefault("_portfolio", {})
            portfolio.setdefault("broker_holdings", {})[ticker] = qty
            return qty
        except Exception as exc:
            print(f"[FILL/BROKER-FB] {ticker} present-balance lookup failed: {exc}")
            return max(
                cached,
                int(states.get(ticker, {}).get("held_quantity", 0) or 0),
            )

    def _resolve_via_broker_holdings(
        self,
        client: Any,
        engine: Any,
        ticker: str,
        runtime: PositionState,
        states: dict[str, Any],
        *,
        broker_qty: int | None,
        cash_after: float | None,
        current_bar_date: str | None,
        inquiry_error: str,
    ) -> bool:
        """
        Infer fill from live broker holdings when ccnl/nccs are unavailable.

        VTS often returns HTTP 500 on inquire-ccnl/nccs while present-balance
        still reflects the filled position.
        """
        if broker_qty is None:
            broker_qty = self._fetch_live_broker_qty(client, ticker, states)
        else:
            broker_qty = max(broker_qty, self._fetch_live_broker_qty(client, ticker, states))

        side = (runtime.open_order_side or "").upper()
        order_qty = int(runtime.open_order_qty or 0)
        odno = runtime.open_order_id or ""
        today = ny_today_yyyymmdd()
        fill_price = float(runtime.open_order_price or 0.0)

        if side == "BUY":
            if broker_qty <= 0:
                return False
            if order_qty > 0 and broker_qty < order_qty:
                runtime.held_quantity = broker_qty
                runtime.in_position = True
                runtime.open_order_filled_qty = broker_qty
                print(
                    f"[FILL/BROKER-FB] {ticker} partial BUY {broker_qty}/{order_qty} "
                    f"(ccnl/nccs unavailable)"
                )
                return False

            runtime.held_quantity = broker_qty
            runtime.in_position = broker_qty > 0
            runtime.open_order_filled_qty = broker_qty
            engine.apply_post_order_transition(
                runtime,
                signal="BUY",
                filled_quantity=runtime.held_quantity,
                current_bar_date=current_bar_date or today,
                allow_crossover=True,
            )
            clear_open_order(runtime)
            self.trade_log.append(
                ticker=ticker,
                signal="BUY",
                qty=runtime.held_quantity,
                order_price=runtime.open_order_price,
                fill_price=fill_price or None,
                status="FILLED",
                reason=f"broker_fallback ccnl/nccs_error={inquiry_error} odno={odno}",
                cash_after=cash_after,
                held_qty=runtime.held_quantity,
            )
            print(
                f"[FILL/BROKER-FB] {ticker} BUY held_qty={runtime.held_quantity} "
                f"confirmed via present-balance odno={odno}"
            )
            self._notify_fill(
                ticker,
                "BUY",
                runtime.held_quantity,
                fill_price,
                "FILLED",
            )
            return True

        if side in {"SELL", "DYNAMIC_ATR_SELL", "PARTIAL_SELL"}:
            pre_held = max(int(runtime.held_quantity or 0), order_qty, broker_qty)
            sold_qty = pre_held - broker_qty if pre_held > broker_qty else order_qty
            if sold_qty <= 0 and broker_qty > 0:
                return False

            runtime.held_quantity = broker_qty
            runtime.open_order_filled_qty = sold_qty
            engine.apply_post_order_transition(
                runtime,
                signal="SELL",
                filled_quantity=sold_qty,
                current_bar_date=current_bar_date or today,
                allow_crossover=True,
            )
            clear_open_order(runtime)
            self.trade_log.append(
                ticker=ticker,
                signal=side,
                qty=sold_qty,
                order_price=runtime.open_order_price,
                fill_price=fill_price or None,
                status="FILLED",
                reason=f"broker_fallback ccnl/nccs_error={inquiry_error} odno={odno}",
                cash_after=cash_after,
                held_qty=runtime.held_quantity,
            )
            print(
                f"[FILL/BROKER-FB] {ticker} SELL sold={sold_qty} "
                f"held_qty={runtime.held_quantity} odno={odno}"
            )
            self._notify_fill(
                ticker,
                side,
                sold_qty,
                fill_price,
                "FILLED",
            )
            return True

        return False

    def resolve_ticker(
        self,
        client: Any,
        engine: Any,
        ticker: str,
        runtime: PositionState,
        states: dict[str, Any],
        *,
        broker_qty: int | None = None,
        cash_after: float | None = None,
        current_bar_date: str | None = None,
    ) -> bool:
        """
        Resolve pending order for one ticker.

        Returns True when pending lock was cleared or updated.
        """
        if not runtime.pending_order and not runtime.open_order_id:
            return False

        odno = runtime.open_order_id
        side = (runtime.open_order_side or "").upper()
        order_qty = int(runtime.open_order_qty or 0)

        if not odno:
            runtime.pending_order = False
            clear_open_order(runtime)
            return True

        ovrs_excg_cd = MARKET_META[ticker]["ovrs_excg_cd"]
        today = ny_today_yyyymmdd()

        try:
            ccnl_rows = client.fetch_overseas_order_ccnl(
                ord_strt_dt=today,
                ord_end_dt=today,
                pdno="",
                ovrs_excg_cd="",
            )
            nccs_rows = client.fetch_overseas_open_orders(ovrs_excg_cd=ovrs_excg_cd)
        except Exception as exc:
            self._notify_fill_inquiry_failure(ticker, exc)
            if self._resolve_via_broker_holdings(
                client,
                engine,
                ticker,
                runtime,
                states,
                broker_qty=broker_qty,
                cash_after=cash_after,
                current_bar_date=current_bar_date,
                inquiry_error=str(exc),
            ):
                self._fill_inquiry_alert_at.pop(ticker, None)
                return True
            return False

        self._fill_inquiry_alert_at.pop(ticker, None)

        filled_qty, fill_price = summarize_ccnl_fills(
            ccnl_rows, odno=odno, ticker=ticker
        )
        still_open = order_still_open(nccs_rows, odno=odno, ticker=ticker)

        if broker_qty is None:
            broker_qty = int(states.get(ticker, {}).get("held_quantity", 0) or 0)

        age = pending_order_age_minutes(runtime)
        cancel_threshold = self.settings.pending_order_cancel_minutes
        stale_threshold = self.settings.pending_order_stale_minutes

        if (
            filled_qty == 0
            and still_open
            and age >= cancel_threshold
        ):
            cancelled = self._try_cancel_open_order(
                client,
                ticker,
                runtime,
                side,
                order_qty,
            )
            release_reason = "cancel_replace" if cancelled else "stale_cancel_failed"
            if age >= stale_threshold or cancelled:
                self._release_stale(
                    runtime,
                    ticker,
                    side,
                    cash_after,
                    reason=release_reason,
                )
                return True
            return False

        if age >= stale_threshold and filled_qty == 0:
            self._try_cancel_open_order(
                client,
                ticker,
                runtime,
                side,
                order_qty,
            )
            self._release_stale(runtime, ticker, side, cash_after, reason="stale_no_fill")
            return True

        if filled_qty <= 0:
            if self._resolve_via_broker_holdings(
                client,
                engine,
                ticker,
                runtime,
                states,
                broker_qty=broker_qty,
                cash_after=cash_after,
                current_bar_date=current_bar_date,
                inquiry_error="ccnl_no_fill_rows",
            ):
                return True
            if still_open:
                print(
                    f"[FILL/PENDING] {ticker} odno={odno} side={side} "
                    f"waiting (0/{order_qty} filled)"
                )
                return False
            print(
                f"[FILL/PENDING] {ticker} odno={odno} side={side} "
                f"no ccnl rows yet — waiting for broker confirmation"
            )
            return False

        if side == "BUY":
            runtime.held_quantity = max(broker_qty, filled_qty)
            runtime.in_position = runtime.held_quantity > 0
            runtime.open_order_filled_qty = filled_qty

            if still_open and filled_qty < order_qty:
                self.trade_log.append(
                    ticker=ticker,
                    signal="BUY",
                    qty=filled_qty,
                    order_price=runtime.open_order_price,
                    fill_price=fill_price or None,
                    status="PARTIAL",
                    reason=f"odno={odno} filled {filled_qty}/{order_qty}",
                    cash_after=cash_after,
                    held_qty=runtime.held_quantity,
                )
                print(
                    f"[FILL/PARTIAL] {ticker} BUY {filled_qty}/{order_qty} "
                    f"@ {fill_price:.2f} odno={odno}"
                )
                self._notify_fill(
                    ticker,
                    "BUY",
                    filled_qty,
                    float(fill_price or runtime.open_order_price or 0.0),
                    "PARTIAL",
                )
                return False

            engine.apply_post_order_transition(
                runtime,
                signal="BUY",
                filled_quantity=runtime.held_quantity,
                current_bar_date=current_bar_date or today,
                allow_crossover=True,
            )
            clear_open_order(runtime)
            self.trade_log.append(
                ticker=ticker,
                signal="BUY",
                qty=runtime.held_quantity,
                order_price=runtime.open_order_price,
                fill_price=fill_price or None,
                status="FILLED",
                reason=f"odno={odno}",
                cash_after=cash_after,
                held_qty=runtime.held_quantity,
            )
            print(
                f"[FILL/COMPLETE] {ticker} BUY held_qty={runtime.held_quantity} "
                f"odno={odno}"
            )
            self._notify_fill(
                ticker,
                "BUY",
                runtime.held_quantity,
                float(fill_price or runtime.open_order_price or 0.0),
                "FILLED",
            )
            return True

        if side in {"SELL", "DYNAMIC_ATR_SELL", "PARTIAL_SELL"}:
            sold_qty = filled_qty if filled_qty > 0 else max(order_qty - broker_qty, 0)
            pre_held = max(broker_qty + sold_qty, runtime.held_quantity)
            runtime.held_quantity = pre_held
            runtime.open_order_filled_qty = sold_qty

            if still_open and broker_qty > 0:
                runtime.held_quantity = broker_qty
                self.trade_log.append(
                    ticker=ticker,
                    signal=side,
                    qty=sold_qty,
                    order_price=runtime.open_order_price,
                    fill_price=fill_price or None,
                    status="PARTIAL",
                    reason=f"odno={odno} sold {sold_qty}/{order_qty}",
                    cash_after=cash_after,
                    held_qty=runtime.held_quantity,
                )
                print(
                    f"[FILL/PARTIAL] {ticker} SELL {sold_qty}/{order_qty} "
                    f"remaining={runtime.held_quantity}"
                )
                self._notify_fill(
                    ticker,
                    side,
                    sold_qty,
                    float(fill_price or runtime.open_order_price or 0.0),
                    "PARTIAL",
                )
                return False

            engine.apply_post_order_transition(
                runtime,
                signal="SELL",
                filled_quantity=sold_qty,
                current_bar_date=current_bar_date or today,
                allow_crossover=True,
            )
            clear_open_order(runtime)
            self.trade_log.append(
                ticker=ticker,
                signal=side,
                qty=sold_qty,
                order_price=runtime.open_order_price,
                fill_price=fill_price or None,
                status="FILLED",
                reason=f"odno={odno}",
                cash_after=cash_after,
                held_qty=runtime.held_quantity,
            )
            print(
                f"[FILL/COMPLETE] {ticker} SELL sold={sold_qty} "
                f"held_qty={runtime.held_quantity}"
            )
            self._notify_fill(
                ticker,
                side,
                sold_qty,
                float(fill_price or runtime.open_order_price or 0.0),
                "FILLED",
            )
            return True

        return False

    def _try_cancel_open_order(
        self,
        client: Any,
        ticker: str,
        runtime: PositionState,
        side: str,
        order_qty: int,
    ) -> bool:
        """Cancel an unfilled broker limit order so the next cycle can re-submit."""
        odno = runtime.open_order_id
        if not odno or not hasattr(client, "cancel_overseas_order"):
            return False
        qty = order_qty or int(runtime.open_order_qty or 0)
        if qty <= 0:
            return False
        try:
            client.cancel_overseas_order(ticker, odno, qty)
            print(
                f"[FILL/CANCEL] {ticker} odno={odno} side={side} "
                f"cancelled after {pending_order_age_minutes(runtime):.0f}m unfilled"
            )
            self._notify_alert(
                "WARNING",
                f"{ticker} limit order {odno} cancelled (unfilled) — will re-submit next cycle",
            )
            return True
        except Exception as exc:
            print(f"[FILL/CANCEL] {ticker} odno={odno} cancel failed: {exc}")
            return False

    def _release_stale(
        self,
        runtime: PositionState,
        ticker: str,
        side: str,
        cash_after: float | None,
        *,
        reason: str,
    ) -> None:
        odno = runtime.open_order_id or ""
        if reason.startswith("cancel") and side == "BUY":
            runtime.pending_resubmit_side = side
            runtime.pending_resubmit_qty = int(runtime.open_order_qty or 0)
        runtime.pending_order = False
        clear_open_order(runtime)
        self.trade_log.append(
            ticker=ticker,
            signal=side or "UNKNOWN",
            qty=0,
            order_price=None,
            fill_price=None,
            status="RELEASED",
            reason=f"{reason} odno={odno}",
            cash_after=cash_after,
            held_qty=int(runtime.held_quantity or 0),
        )
        print(f"[FILL/RELEASE] {ticker} pending cleared ({reason}) odno={odno}")

    def resolve_all_pending(
        self,
        client: Any,
        states: dict[str, Any],
        watchlist: list[str],
        engine_factory: Any,
        *,
        cash_after: float | None = None,
    ) -> None:
        broker_holdings = (
            states.get("_portfolio", {}).get("broker_holdings", {}) or {}
        )
        for ticker in watchlist:
            payload = states.get(ticker, {})
            if not isinstance(payload, dict):
                continue
            runtime = PositionState.from_dict(payload)
            if not runtime.pending_order and not runtime.open_order_id:
                continue
            engine = engine_factory(ticker)
            broker_qty = int(broker_holdings.get(ticker, runtime.held_quantity) or 0)
            changed = self.resolve_ticker(
                client,
                engine,
                ticker,
                runtime,
                states,
                broker_qty=broker_qty,
                cash_after=cash_after,
            )
            if changed or runtime.pending_order:
                states[ticker] = engine.dump_state(runtime)
