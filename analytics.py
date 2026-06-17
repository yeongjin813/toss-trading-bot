from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pandas as pd
import pytz

from config import StrategyConfig, StrategyConfigMapper

try:
    import holidays as holidays_lib
except ImportError:  # pragma: no cover - optional until pip install
    holidays_lib = None

StrategyConfigRegistry = StrategyConfigMapper

NY_TZ = pytz.timezone("America/New_York")
US_REGULAR_MARKET_OPEN = time(hour=9, minute=30)
US_REGULAR_MARKET_CLOSE = time(hour=16, minute=0)

_holiday_cross_check_years: set[int] = set()


def use_eod_atr_stops() -> bool:
    """
    When true, live ATR stops use the static daily bar low only (backtest parity).

    Default false preserves legacy intraday ``session_low`` scanning.
    """
    return os.getenv("USE_EOD_ATR_STOPS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class IndicatorAnalytics:
    """
    Stateless dual-moving-average indicator pipeline.

    populate_indicators() accepts the ticker-isolated sma_period from the strategy
    layer and always computes sma_long (50-day regime baseline) alongside RSI,
    ATR, and volume MA.
    """

    @staticmethod
    def populate_indicators(
        df: pd.DataFrame,
        config: StrategyConfig,
    ) -> pd.DataFrame:
        """
        Augment OHLCV with short SMA, long SMA, RSI, ATR, Volume SMA.

        SMA_SHORT window = config.sma_period (ticker-isolated entry/exit cross).
        SMA_LONG  window = config.sma_long_period (fixed 50-day regime filter).
        """
        enriched = df.copy()
        enriched["SMA_SHORT"] = calculate_sma(enriched, config.sma_period)
        enriched["SMA_LONG"] = calculate_sma(enriched, config.sma_long_period)
        enriched["RSI"] = calculate_rsi(enriched, config.rsi_period)
        enriched["ATR"] = calculate_atr(enriched, config.atr_period)
        enriched["Volume_SMA"] = calculate_sma(
            enriched,
            config.volume_sma_period,
            column="Volume",
        )

        if enriched.isna().any().any():
            lookback = max(
                config.sma_period,
                config.sma_long_period,
                config.rsi_period,
                config.atr_period,
                config.volume_sma_period,
            )
            enriched = enriched.iloc[lookback:].dropna().reset_index(drop=True)

        return enriched


@dataclass
class PositionState:
    """
    Unified runtime registry: position, trailing stop, and order lifecycle.

    Persisted fields include has_position (in_position), pending_order,
    last_processed_date, latest_bar_date, held_quantity, and session_low.
    """

    in_position: bool = False
    pending_order: bool = False
    last_processed_date: str | None = None
    latest_bar_date: str | None = None
    held_quantity: int = 0
    last_failed_order_key: str | None = None
    open_order_id: str | None = None
    open_order_side: str | None = None
    open_order_qty: int = 0
    open_order_price: float | None = None
    open_order_submitted_at: str | None = None
    open_order_filled_qty: int = 0
    session_low: float | None = None
    highest_price_achieved: float | None = None
    current_atr: float | None = None
    dynamic_stop_distance: float | None = None
    trigger_floor: float | None = None

    @property
    def has_position(self) -> bool:
        return self.in_position

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> PositionState:
        if not payload:
            return cls()

        valid = {field.name for field in fields(cls)}
        filtered = {key: payload[key] for key in payload if key in valid}
        if "held_quantity" in filtered:
            filtered["held_quantity"] = int(filtered["held_quantity"] or 0)
        if "in_position" in filtered:
            filtered["in_position"] = bool(filtered["in_position"])
        if "pending_order" in filtered:
            filtered["pending_order"] = bool(filtered["pending_order"])
        if "open_order_qty" in filtered:
            filtered["open_order_qty"] = int(filtered["open_order_qty"] or 0)
        if "open_order_filled_qty" in filtered:
            filtered["open_order_filled_qty"] = int(
                filtered["open_order_filled_qty"] or 0
            )
        if "open_order_price" in filtered and filtered["open_order_price"] not in (
            None,
            "",
        ):
            filtered["open_order_price"] = float(filtered["open_order_price"])
        elif "open_order_price" in filtered:
            filtered["open_order_price"] = None
        return cls(**filtered)


@dataclass
class BarSnapshot:
    date: Any
    open: float
    high: float
    low: float
    close: float
    volume: float
    sma_short: float
    sma_long: float
    rsi: float
    atr: float
    volume_sma: float

    @property
    def sma(self) -> float:
        """Alias for sma_short — preserves legacy telemetry consumers."""
        return self.sma_short

    @classmethod
    def from_row(cls, row: pd.Series) -> BarSnapshot:
        return cls(
            date=pd.Timestamp(row["Date"]).date(),
            open=float(row["Open"]),
            high=float(row["High"]),
            low=float(row["Low"]),
            close=float(row["Close"]),
            volume=float(row["Volume"]),
            sma_short=float(row["SMA_SHORT"]),
            sma_long=float(row["SMA_LONG"]),
            rsi=float(row["RSI"]),
            atr=float(row["ATR"]),
            volume_sma=float(row["Volume_SMA"]),
        )

    def to_metrics_dict(self) -> dict[str, float | Any]:
        return {
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "sma": self.sma_short,
            "sma_short": self.sma_short,
            "sma_long": self.sma_long,
            "rsi": self.rsi,
            "atr": self.atr,
            "volume_sma": self.volume_sma,
        }


def _format_bar_date(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _wilder_ewm(series: pd.Series, period: int) -> pd.Series:
    alpha = 1.0 / period
    return series.ewm(alpha=alpha, adjust=False).mean()


def calculate_sma(df: pd.DataFrame, period: int, column: str = "Close") -> pd.Series:
    return df[column].rolling(window=period).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = _wilder_ewm(gain, period)
    avg_loss = _wilder_ewm(loss, period)

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr_components = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    )
    true_range = tr_components.max(axis=1)
    return _wilder_ewm(true_range, period)


def enrich_with_indicators(
    df: pd.DataFrame,
    config: StrategyConfig,
) -> pd.DataFrame:
    """Backward-compatible wrapper delegating to IndicatorAnalytics."""
    return IndicatorAnalytics.populate_indicators(df, config)


def calculate_position_size(
    capital_at_risk: float,
    risk_per_trade: float,
    entry_price: float,
    stop_distance: float,
    available_capital: float | None = None,
) -> int:
    """
    Dual-clamp position sizing with a 5% deployable capital friction buffer.

    shares_by_risk = int(dollar_risk / stop_distance)
    shares_by_capital = int((deployable * 0.95) / entry_price)
    final_shares = max(1, min(shares_by_risk, shares_by_capital))
    """
    if entry_price <= 0 or stop_distance <= 0 or risk_per_trade <= 0:
        return 0

    deployable = (
        available_capital if available_capital is not None else capital_at_risk
    )
    if deployable <= 0:
        return 0

    dollar_risk = capital_at_risk * risk_per_trade
    shares_by_risk = int(dollar_risk / stop_distance)
    shares_by_capital = int((deployable * 0.95) / entry_price)

    if shares_by_risk <= 0 or shares_by_capital <= 0:
        return 0

    return max(1, min(shares_by_risk, shares_by_capital))


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    """NYSE observed-date rule for fixed calendar holidays."""
    observed = date(year, month, day)
    if observed.weekday() == 5:
        return observed - timedelta(days=1)
    if observed.weekday() == 6:
        return observed + timedelta(days=1)
    return observed


def _memorial_day(year: int) -> date:
    observed = date(year, 5, 31)
    while observed.weekday() != 0:
        observed -= timedelta(days=1)
    return observed


def _labor_day(year: int) -> date:
    observed = date(year, 9, 1)
    while observed.weekday() != 0:
        observed += timedelta(days=1)
    return observed


def _thanksgiving_day(year: int) -> date:
    observed = date(year, 11, 1)
    while observed.weekday() != 3:
        observed += timedelta(days=1)
    return observed + timedelta(weeks=3)


def _nyse_library_holidays(year: int) -> dict[date, str]:
    if holidays_lib is None:
        return {}
    calendar = holidays_lib.NYSE(years=year)
    return {day: str(name) for day, name in sorted(calendar.items())}


def _cross_check_holiday_calendars(
    year: int,
    internal: dict[date, str],
    library: dict[date, str],
) -> dict[date, str]:
    """Merge ``holidays.NYSE`` closures and log calendar drift once per year."""
    if year in _holiday_cross_check_years:
        return {}

    _holiday_cross_check_years.add(year)
    if not library:
        print(f"[CALENDAR/WARN] holidays.NYSE unavailable — internal registry only ({year})")
        return {}

    added = sorted(day for day in library if day not in internal)
    for day in added:
        print(f"[CALENDAR] Added NYSE closure from library: {day} ({library[day]})")

    internal_only = sorted(day for day in internal if day not in library)
    for day in internal_only:
        print(
            f"[CALENDAR/WARN] Internal holiday not in holidays.NYSE: "
            f"{day} ({internal[day]})"
        )

    return {day: library[day] for day in added}


def us_market_holidays_for_year(year: int) -> dict[date, str]:
    """Return NYSE full-day closure dates for a calendar year."""
    internal = {
        _observed_fixed_holiday(year, 1, 1): "New Year's Day",
        _memorial_day(year): "Memorial Day",
        _observed_fixed_holiday(year, 7, 4): "Independence Day",
        _labor_day(year): "Labor Day",
        _thanksgiving_day(year): "Thanksgiving",
        _observed_fixed_holiday(year, 12, 25): "Christmas",
    }
    library = _nyse_library_holidays(year)
    _cross_check_holiday_calendars(year, internal, library)
    merged = dict(internal)
    merged.update(library)
    return merged


def is_us_market_holiday(day: date | datetime | None = None) -> bool:
    """True when the given date is a registered US equity market holiday."""
    current = day.date() if isinstance(day, datetime) else (day or date.today())
    return current in us_market_holidays_for_year(current.year)


def _to_ny_datetime(current_dt: datetime | None) -> datetime:
    """Convert any aware/naive datetime to America/New_York (DST-safe via pytz)."""
    if current_dt is None:
        return datetime.now(NY_TZ)

    if current_dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        current_dt = current_dt.replace(tzinfo=local_tz)

    return current_dt.astimezone(NY_TZ)


def is_us_regular_market_hours(current_dt: datetime | None = None) -> bool:
    """
    True during NYSE regular session: Mon-Fri 09:30-16:00 America/New_York.

    Uses pytz for DST transitions - no hardcoded Korea/local offset math.
    """
    ny_dt = _to_ny_datetime(current_dt)

    if ny_dt.weekday() >= 5:
        return False

    if ny_dt.date() in us_market_holidays_for_year(ny_dt.year):
        return False

    current_clock = ny_dt.time()
    return US_REGULAR_MARKET_OPEN <= current_clock < US_REGULAR_MARKET_CLOSE


def seconds_until_us_rth_open(
    current_dt: datetime | None = None,
    *,
    min_sleep: int = 60,
    max_sleep: int = 3600,
) -> int:
    """
    Seconds until the next NYSE regular session open (09:30 ET).

    Capped at ``max_sleep`` so the outer loop can re-check clocks periodically.
    """
    if is_us_regular_market_hours(current_dt):
        return 0

    ny_dt = _to_ny_datetime(current_dt)
    candidate_day = ny_dt.date()
    if ny_dt.time() >= US_REGULAR_MARKET_CLOSE:
        candidate_day += timedelta(days=1)

    open_dt: datetime | None = None
    for _ in range(366):
        if candidate_day.weekday() >= 5:
            candidate_day += timedelta(days=1)
            continue
        if candidate_day in us_market_holidays_for_year(candidate_day.year):
            candidate_day += timedelta(days=1)
            continue

        open_dt = ny_dt.replace(
            year=candidate_day.year,
            month=candidate_day.month,
            day=candidate_day.day,
            hour=US_REGULAR_MARKET_OPEN.hour,
            minute=US_REGULAR_MARKET_OPEN.minute,
            second=0,
            microsecond=0,
        )
        if open_dt <= ny_dt:
            candidate_day += timedelta(days=1)
            continue
        break

    if open_dt is None or open_dt <= ny_dt:
        return max_sleep

    seconds = int((open_dt - ny_dt).total_seconds())
    return min(max(seconds, min_sleep), max_sleep)


def ny_regular_session_elapsed_fraction(current_dt: datetime | None = None) -> float:
    """
    Fraction of the NY regular session elapsed (09:30-16:00 ET).

    Used to scale the volume gate during intraday polls so partial-bar volume
    is not compared against a full-day 20-bar average without adjustment.
    """
    if not is_us_regular_market_hours(current_dt):
        return 1.0

    ny_dt = _to_ny_datetime(current_dt)
    session_open = ny_dt.replace(
        hour=US_REGULAR_MARKET_OPEN.hour,
        minute=US_REGULAR_MARKET_OPEN.minute,
        second=0,
        microsecond=0,
    )
    session_close = ny_dt.replace(
        hour=US_REGULAR_MARKET_CLOSE.hour,
        minute=US_REGULAR_MARKET_CLOSE.minute,
        second=0,
        microsecond=0,
    )
    total_seconds = (session_close - session_open).total_seconds()
    if total_seconds <= 0:
        return 1.0

    elapsed = (ny_dt - session_open).total_seconds()
    return min(max(elapsed / total_seconds, 0.05), 1.0)


def describe_us_market_closure(now: datetime | None = None) -> str | None:
    """Return a closure reason string, or None when the calendar session is open."""
    ny_dt = _to_ny_datetime(now)

    if ny_dt.weekday() >= 5:
        return "weekend"

    holiday_name = us_market_holidays_for_year(ny_dt.year).get(ny_dt.date())
    if holiday_name:
        return holiday_name
    return None


def is_us_equity_session(now: datetime | None = None) -> bool:
    """Calendar session gate: NY weekday excluding registered market holidays."""
    return describe_us_market_closure(now) is None


def should_allow_crossover_signals(
    current_bar_date: str,
    last_processed_date: str | None,
    regular_market_hours: bool,
) -> bool:
    """
    Crossover BUY/SELL allowed only during regular NY hours on a new daily bar.

    Pre-market, post-market, weekends, and holidays force crossover off.
    """
    if not regular_market_hours:
        return False
    if last_processed_date is None:
        return True
    return current_bar_date != last_processed_date


def build_spy_regime_lookup(
    spy_df: pd.DataFrame,
    sma_period: int = 200,
) -> dict[str, bool]:
    """Map YYYY-MM-DD -> True when SPY close is above its long SMA (bull regime)."""
    frame = spy_df.copy()
    if "Date" in frame.columns:
        frame["Date"] = pd.to_datetime(frame["Date"])
        frame = frame.sort_values("Date").set_index("Date")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)

    frame = frame.sort_index()
    frame["BenchSMA"] = frame["Close"].rolling(window=sma_period, min_periods=sma_period).mean()
    lookup: dict[str, bool] = {}
    for ts, row in frame.iterrows():
        sma_val = row.get("BenchSMA")
        if pd.isna(sma_val):
            continue
        lookup[pd.Timestamp(ts).strftime("%Y-%m-%d")] = float(row["Close"]) > float(sma_val)
    return lookup


def resolve_spy_market_bullish(
    spy_lookup: dict[str, bool] | None,
    bar_date: str,
    *,
    default: bool = True,
) -> bool:
    if not spy_lookup:
        return default
    return spy_lookup.get(bar_date, default)


def spy_regime_snapshot(
    spy_df: pd.DataFrame,
    bar_date: str,
    sma_period: int = 200,
) -> tuple[bool, float | None, float | None]:
    """Return (bullish, spy_close, spy_sma) for telemetry on a given session date."""
    frame = spy_df.copy()
    if "Date" in frame.columns:
        frame["Date"] = pd.to_datetime(frame["Date"])
        frame = frame.sort_values("Date").set_index("Date")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()
    frame["BenchSMA"] = frame["Close"].rolling(window=sma_period, min_periods=sma_period).mean()

    target = pd.Timestamp(bar_date)
    if target not in frame.index:
        prior = frame.index[frame.index <= target]
        if len(prior) == 0:
            return True, None, None
        target = prior[-1]

    row = frame.loc[target]
    spy_close = float(row["Close"])
    spy_sma = row.get("BenchSMA")
    if pd.isna(spy_sma):
        return True, spy_close, None
    spy_sma = float(spy_sma)
    return spy_close > spy_sma, spy_close, spy_sma


class LiveSignalEngine:
    """
    Ticker-bound live signal engine with regime-isolated configuration.

    Entry (all required):
      Golden Cross AND RSI >= rsi_buy_threshold AND Volume > threshold * Volume_SMA
      AND (if use_trend_filter: Close > sma_long OR sma_short > sma_long)

    Exit priority when positioned:
      1. DYNAMIC_ATR_SELL (prior trigger_floor vs bar low)
      2. Death Cross (unconditional emergency exit)
      3. RSI crossdown through rsi_exit_threshold (50) — conditional:
         - use_trend_filter=False: always eligible (breakout regime)
         - use_trend_filter=True: only when Close < sma_long (weak/bearish regime)
    """

    def __init__(self, ticker: str, config: StrategyConfig | None = None) -> None:
        self.ticker = ticker.strip().upper()
        self.config = config or StrategyConfigMapper.for_ticker(self.ticker)

    def enrich(self, df: pd.DataFrame) -> pd.DataFrame:
        enriched = IndicatorAnalytics.populate_indicators(df, self.config)
        if len(enriched) < 2:
            raise ValueError("Insufficient data length for signal evaluation.")
        return enriched

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute short SMA, 50-day SMA, RSI, ATR, and Volume SMA."""
        return self.enrich(df)

    def load_state(self, payload: dict[str, Any] | None) -> PositionState:
        return PositionState.from_dict(payload)

    def dump_state(self, state: PositionState) -> dict[str, Any]:
        return state.to_dict()

    def _has_active_position(self, state: PositionState) -> bool:
        """Position path for replay/intraday logic (dispatch still requires held_quantity > 0)."""
        return state.in_position or state.held_quantity > 0

    def _passes_volume_filter(
        self,
        volume: float,
        volume_sma: float,
        *,
        session_volume_fraction: float = 1.0,
    ) -> bool:
        if volume_sma <= 0:
            return False
        fraction = min(max(session_volume_fraction, 0.05), 1.0)
        effective_threshold = self.config.volume_threshold * fraction
        return volume > volume_sma * effective_threshold

    def _golden_cross(
        self, close: float, sma: float, prev_close: float, prev_sma: float
    ) -> bool:
        return close > sma and prev_close <= prev_sma

    def _death_cross(
        self, close: float, sma: float, prev_close: float, prev_sma: float
    ) -> bool:
        return close < sma and prev_close >= prev_sma

    def _rsi_allows_entry(self, rsi: float) -> bool:
        return rsi >= self.config.rsi_buy_threshold

    def _passes_trend_filter(self, bar: BarSnapshot) -> bool:
        """
        Regime gate for entries when use_trend_filter=True.

        Permits entry when price is above the 50-day baseline OR short SMA is
        above the 50-day baseline — captures strong trends before full price
        confirmation while blocking counter-trend whipsaws.
        """
        if not self.config.use_trend_filter:
            return True
        return bar.close > bar.sma_long or bar.sma_short > bar.sma_long

    def _in_weak_regime(self, bar: BarSnapshot) -> bool:
        """Weak/bearish regime: current_close < sma_long."""
        return bar.close < bar.sma_long

    def _rsi_crossdown_exit(self, rsi: float, prev_rsi: float) -> bool:
        """
        RSI crossdown exit at rsi_exit_threshold (default 50).

        Triggers when RSI_{t-1} >= threshold AND RSI_t < threshold.
        """
        threshold = self.config.rsi_exit_threshold
        return prev_rsi >= threshold and rsi < threshold

    def _rsi_exit_allowed(self, bar: BarSnapshot) -> bool:
        """
        Conditional RSI exit gate (whipsaw protection during bull runs).

        - use_trend_filter=False (PLTR): RSI crossdown always eligible.
        - use_trend_filter=True (NVDA/DEFAULT): RSI crossdown only in weak regime
          (current_close < sma_long). Death cross remains unconditional.
        """
        if not self.config.use_trend_filter:
            return True
        return self._in_weak_regime(bar)

    def _update_trailing_state(
        self, state: PositionState, close: float, atr: float
    ) -> None:
        if not state.in_position:
            return

        if (
            state.highest_price_achieved is None
            or close > state.highest_price_achieved
        ):
            state.highest_price_achieved = close

        state.current_atr = atr
        state.dynamic_stop_distance = atr * self.config.atr_multiplier
        state.trigger_floor = state.highest_price_achieved - state.dynamic_stop_distance

    def _dynamic_atr_stop_triggered(
        self, state: PositionState, bar_low: float
    ) -> bool:
        if not self.config.use_trailing_stop or not state.in_position:
            return False
        if state.trigger_floor is None:
            return False
        return bar_low <= state.trigger_floor

    def _build_dynamic_atr_payload(
        self,
        state: PositionState,
        bar: BarSnapshot,
        session_low: float,
    ) -> dict[str, float]:
        return {
            "captured_peak": float(state.highest_price_achieved or bar.close),
            "current_atr": float(state.current_atr or bar.atr),
            "dynamic_stop_distance": float(state.dynamic_stop_distance or 0.0),
            "trigger_floor": float(state.trigger_floor or 0.0),
            "session_low": float(session_low),
            "current_execution_price": bar.close,
        }

    def evaluate_intraday_atr_stop(
        self,
        state: PositionState,
        bar: BarSnapshot,
        session_low: float,
        mutate_state: bool = False,
    ) -> dict[str, Any] | None:
        """
        Priority-1 intraday scan: DYNAMIC_ATR_SELL when Low_t <= trigger floor.

        Uses the running session_low for the active daily bar, not only the
        static EOD low stored in the historical dataframe row.
        """
        if not state.in_position or state.pending_order:
            return None

        if not self._has_active_position(state):
            return None

        working = PositionState.from_dict(state.to_dict())

        if self._dynamic_atr_stop_triggered(working, session_low):
            dynamic_atr_stop = self._build_dynamic_atr_payload(
                working, bar, session_low
            )
            if mutate_state:
                state.in_position = False
                state.held_quantity = 0
                state.highest_price_achieved = None
                state.current_atr = None
                state.dynamic_stop_distance = None
                state.trigger_floor = None
                state.session_low = None
            return {
                "signal": "DYNAMIC_ATR_SELL",
                "dynamic_atr_stop": dynamic_atr_stop,
                "liquidity_ok": True,
            }

        self._update_trailing_state(working, bar.close, bar.atr)
        if mutate_state:
            state.highest_price_achieved = working.highest_price_achieved
            state.current_atr = working.current_atr
            state.dynamic_stop_distance = working.dynamic_stop_distance
            state.trigger_floor = working.trigger_floor
        return None

    def evaluate_bar(
        self,
        state: PositionState,
        bar: BarSnapshot,
        prev_bar: BarSnapshot,
        mutate_state: bool = True,
        allow_crossover: bool = True,
        session_low: float | None = None,
        session_volume_fraction: float = 1.0,
        market_bullish: bool = True,
    ) -> dict[str, Any]:
        """
        Evaluate one bar using unified priority:
        DYNAMIC_ATR_SELL -> SELL -> BUY -> HOLD.

        Position-active sequence (no look-ahead):
          1. ATR stop vs prior trigger_floor and current low
          2. Update trailing peak/floor with close only after survival
          3. Death cross (unconditional) or conditional RSI crossdown exit
        """
        liquidity_ok = self._passes_volume_filter(
            bar.volume,
            bar.volume_sma,
            session_volume_fraction=session_volume_fraction,
        )
        effective_low = session_low if session_low is not None else bar.low

        working_state = (
            PositionState.from_dict(state.to_dict()) if not mutate_state else state
        )
        dynamic_atr_stop: dict[str, float] | None = None

        if self._has_active_position(working_state):
            if self._dynamic_atr_stop_triggered(working_state, effective_low):
                dynamic_atr_stop = self._build_dynamic_atr_payload(
                    working_state, bar, effective_low
                )
                if mutate_state:
                    state.in_position = False
                    state.held_quantity = 0
                    state.highest_price_achieved = None
                    state.current_atr = None
                    state.dynamic_stop_distance = None
                    state.trigger_floor = None
                    state.session_low = None
                return {
                    "signal": "DYNAMIC_ATR_SELL",
                    "dynamic_atr_stop": dynamic_atr_stop,
                    "liquidity_ok": liquidity_ok,
                    "state": working_state.to_dict(),
                }

            self._update_trailing_state(working_state, bar.close, bar.atr)
            dynamic_atr_stop = self._build_dynamic_atr_payload(
                working_state, bar, effective_low
            )

            if not allow_crossover:
                return {
                    "signal": "HOLD",
                    "dynamic_atr_stop": dynamic_atr_stop,
                    "liquidity_ok": liquidity_ok,
                    "crossover_suppressed": True,
                    "state": working_state.to_dict(),
                }

            cross_below = self._death_cross(
                bar.close, bar.sma_short, prev_bar.close, prev_bar.sma_short
            )
            rsi_exit = (
                self._rsi_exit_allowed(bar)
                and self._rsi_crossdown_exit(bar.rsi, prev_bar.rsi)
            )
            if cross_below or rsi_exit:
                if mutate_state:
                    state.in_position = False
                    state.held_quantity = 0
                    state.highest_price_achieved = None
                    state.current_atr = None
                    state.dynamic_stop_distance = None
                    state.trigger_floor = None
                    state.session_low = None
                return {
                    "signal": "SELL",
                    "dynamic_atr_stop": dynamic_atr_stop,
                    "liquidity_ok": liquidity_ok,
                    "state": working_state.to_dict(),
                }

            return {
                "signal": "HOLD",
                "dynamic_atr_stop": dynamic_atr_stop,
                "liquidity_ok": liquidity_ok,
                "state": working_state.to_dict(),
            }

        if not allow_crossover:
            return {
                "signal": "HOLD",
                "dynamic_atr_stop": None,
                "liquidity_ok": liquidity_ok,
                "crossover_suppressed": True,
                "state": working_state.to_dict(),
            }

        cross_above = self._golden_cross(
            bar.close, bar.sma_short, prev_bar.close, prev_bar.sma_short
        )
        trend_ok = self._passes_trend_filter(bar)
        if (
            cross_above
            and self._rsi_allows_entry(bar.rsi)
            and liquidity_ok
            and trend_ok
            and market_bullish
        ):
            if mutate_state:
                state.in_position = True
                state.highest_price_achieved = bar.close
                self._update_trailing_state(state, bar.close, bar.atr)
            return {
                "signal": "BUY",
                "dynamic_atr_stop": None,
                "liquidity_ok": True,
                "state": (
                    state.to_dict()
                    if mutate_state
                    else working_state.to_dict()
                ),
            }

        hold_reason: dict[str, Any] = {}
        if cross_above and self._rsi_allows_entry(bar.rsi) and not liquidity_ok:
            hold_reason["liquidity_blocked"] = True
        elif cross_above and self._rsi_allows_entry(bar.rsi) and not trend_ok:
            hold_reason["trend_filter_blocked"] = True
        elif (
            cross_above
            and self._rsi_allows_entry(bar.rsi)
            and liquidity_ok
            and trend_ok
            and not market_bullish
        ):
            hold_reason["market_filter_blocked"] = True

        return {
            "signal": "HOLD",
            "dynamic_atr_stop": None,
            "liquidity_ok": liquidity_ok,
            **hold_reason,
            "state": working_state.to_dict(),
        }

    def replay_state(
        self,
        enriched: pd.DataFrame,
        end_index: int | None = None,
        initial_state: dict[str, Any] | None = None,
    ) -> PositionState:
        """Replay history to derive O(1)-ready state without full recompute at runtime."""
        if len(enriched) < 2:
            raise ValueError("Insufficient data length for signal evaluation.")

        state = self.load_state(initial_state)
        last_index = len(enriched) - 1 if end_index is None else end_index

        for i in range(1, last_index + 1):
            bar = BarSnapshot.from_row(enriched.iloc[i])
            prev_bar = BarSnapshot.from_row(enriched.iloc[i - 1])
            self.evaluate_bar(
                state,
                bar,
                prev_bar,
                mutate_state=True,
                allow_crossover=True,
            )

        return state

    def update_session_low(
        self, runtime: PositionState, bar: BarSnapshot
    ) -> float:
        """Track the minimum low observed for the active daily bar."""
        bar_date = _format_bar_date(bar.date)
        if runtime.latest_bar_date != bar_date:
            runtime.latest_bar_date = bar_date
            runtime.session_low = bar.low
        elif runtime.session_low is None:
            runtime.session_low = bar.low
        else:
            runtime.session_low = min(runtime.session_low, bar.low)
        return float(runtime.session_low)

    def evaluate_trading_cycle(
        self,
        df: pd.DataFrame,
        runtime_state: dict[str, Any] | None,
        capital_at_risk: float,
        risk_per_trade: float,
        *,
        now: datetime | None = None,
        available_capital: float | None = None,
        portfolio_equity: float | None = None,
        current_price: float | None = None,
        market_bullish: bool = True,
    ) -> dict[str, Any]:
        """
        Evaluate one live monitoring cycle with bar-trailing and session gates.

        Returns signal_result, metrics, sizing, and the non-mutated replay snapshot.

        When available_capital / portfolio_equity are supplied (post-reconciliation),
        position sizing uses broker-aligned deployable cash and total equity.
        """
        enriched = self.enrich(df)
        replay_end = len(enriched) - 2

        if replay_end < 1:
            raise ValueError("Insufficient data length for signal evaluation.")

        runtime = self.load_state(runtime_state)
        position_state = self.replay_state(
            enriched,
            end_index=replay_end,
            initial_state={
                key: runtime_state.get(key)
                for key in PositionState.from_dict(runtime_state).to_dict()
                if key
                not in {
                    "pending_order",
                    "last_processed_date",
                    "latest_bar_date",
                    "held_quantity",
                    "session_low",
                }
            }
            if runtime_state
            else None,
        )

        runtime.in_position = position_state.in_position
        runtime.highest_price_achieved = position_state.highest_price_achieved
        runtime.current_atr = position_state.current_atr
        runtime.dynamic_stop_distance = position_state.dynamic_stop_distance
        runtime.trigger_floor = position_state.trigger_floor
        if runtime.held_quantity <= 0:
            runtime.in_position = False

        today = BarSnapshot.from_row(enriched.iloc[-1])
        yesterday = BarSnapshot.from_row(enriched.iloc[-2])
        current_bar_date = _format_bar_date(today.date)

        calendar_open = is_us_equity_session(now)
        regular_market_hours = is_us_regular_market_hours(now)

        mark_price = current_price if current_price is not None else today.close
        if use_eod_atr_stops():
            session_low = float(today.low)
        elif regular_market_hours:
            bar_date_str = current_bar_date
            tick_low = min(float(today.low), float(mark_price))
            if runtime.latest_bar_date != bar_date_str:
                runtime.latest_bar_date = bar_date_str
                runtime.session_low = tick_low
            elif runtime.session_low is None:
                runtime.session_low = tick_low
            else:
                runtime.session_low = min(float(runtime.session_low), tick_low)
            session_low = float(
                runtime.session_low if runtime.session_low is not None else today.low
            )
        else:
            session_low = float(
                runtime.session_low if runtime.session_low is not None else today.low
            )
        allow_crossover = should_allow_crossover_signals(
            current_bar_date,
            runtime.last_processed_date,
            regular_market_hours,
        )
        session_volume_fraction = (
            ny_regular_session_elapsed_fraction(now)
            if regular_market_hours
            else 1.0
        )

        eval_state = PositionState.from_dict(position_state.to_dict())
        eval_state.pending_order = runtime.pending_order
        eval_state.last_processed_date = runtime.last_processed_date
        eval_state.latest_bar_date = runtime.latest_bar_date
        eval_state.held_quantity = runtime.held_quantity
        eval_state.session_low = runtime.session_low
        if eval_state.held_quantity <= 0:
            eval_state.in_position = False

        signal_result: dict[str, Any]
        if runtime.pending_order:
            signal_result = {
                "signal": "HOLD",
                "liquidity_ok": self._passes_volume_filter(
                    today.volume,
                    today.volume_sma,
                    session_volume_fraction=session_volume_fraction,
                ),
                "pending_order_locked": True,
            }
        elif not regular_market_hours:
            signal_result = {
                "signal": "HOLD",
                "liquidity_ok": self._passes_volume_filter(
                    today.volume,
                    today.volume_sma,
                    session_volume_fraction=1.0,
                ),
                "crossover_suppressed": True,
                "outside_regular_hours": True,
            }
        else:
            signal_result = self.evaluate_bar(
                eval_state,
                today,
                yesterday,
                mutate_state=False,
                allow_crossover=allow_crossover,
                session_low=session_low,
                session_volume_fraction=session_volume_fraction,
                market_bullish=market_bullish,
            )

        if (
            not regular_market_hours
            and signal_result.get("signal") in {"BUY", "SELL", "DYNAMIC_ATR_SELL"}
        ):
            signal_result = {
                "signal": "HOLD",
                "liquidity_ok": signal_result.get("liquidity_ok", False),
                "crossover_suppressed": True,
                "outside_regular_hours": True,
            }

        if (
            signal_result.get("signal") in {"SELL", "DYNAMIC_ATR_SELL"}
            and runtime.held_quantity <= 0
        ):
            signal_result = {
                **signal_result,
                "signal": "HOLD",
                "liquidation_blocked": True,
            }
            runtime.in_position = False

        stop_distance = today.atr * self.config.atr_multiplier
        risk_base = portfolio_equity if portfolio_equity is not None else capital_at_risk
        deploy_capital = (
            available_capital if available_capital is not None else capital_at_risk
        )
        position_size = calculate_position_size(
            capital_at_risk=risk_base,
            risk_per_trade=risk_per_trade,
            entry_price=today.close,
            stop_distance=stop_distance,
            available_capital=deploy_capital,
        )

        return {
            "metrics": {
                "today": today.to_metrics_dict(),
                "yesterday": yesterday.to_metrics_dict(),
            },
            "runtime_state": runtime.to_dict(),
            "position_state": position_state.to_dict(),
            "signal_result": signal_result,
            "position_size": position_size,
            "current_bar_date": current_bar_date,
            "session_low": session_low,
            "eod_atr_stops": use_eod_atr_stops(),
            "allow_crossover": allow_crossover,
            "market_bullish": market_bullish,
            "calendar_open": calendar_open,
            "regular_market_hours": regular_market_hours,
            "market_open": regular_market_hours,
            "state_snapshot": self.dump_state(position_state),
            "ticker_config": {
                "ticker": self.config.ticker,
                "sma_period": self.config.sma_period,
                "atr_multiplier": self.config.atr_multiplier,
                "use_trend_filter": self.config.use_trend_filter,
            },
        }

    def apply_post_order_transition(
        self,
        runtime: PositionState,
        signal: str,
        filled_quantity: int,
        current_bar_date: str,
        allow_crossover: bool,
    ) -> None:
        """Mutate runtime registry after a confirmed KIS order (rt_cd == 0)."""
        if signal == "BUY":
            runtime.in_position = filled_quantity > 0
            runtime.held_quantity = filled_quantity
            runtime.pending_order = False
            runtime.last_processed_date = current_bar_date
            return

        if signal in {"SELL", "DYNAMIC_ATR_SELL"}:
            remaining = max(int(runtime.held_quantity) - int(filled_quantity), 0)
            runtime.in_position = remaining > 0
            runtime.held_quantity = remaining
            if remaining <= 0:
                runtime.highest_price_achieved = None
                runtime.current_atr = None
                runtime.dynamic_stop_distance = None
                runtime.trigger_floor = None
                runtime.session_low = None
            runtime.pending_order = False
            runtime.last_processed_date = current_bar_date
            return

        runtime.pending_order = False

    def evaluate_session(
        self,
        df: pd.DataFrame,
        external_state: dict[str, Any] | None = None,
        end_index: int | None = None,
        capital_at_risk: float = 10_000.0,
        risk_per_trade: float = 0.01,
    ) -> dict[str, Any]:
        """Backward-compatible session wrapper used by tests and legacy callers."""
        cycle = self.evaluate_trading_cycle(
            df,
            runtime_state=external_state,
            capital_at_risk=capital_at_risk,
            risk_per_trade=risk_per_trade,
        )
        return {
            "metrics": cycle["metrics"],
            "position_state": cycle["position_state"],
            "signal_result": cycle["signal_result"],
            "position_size": cycle["position_size"],
            "state_snapshot": cycle["state_snapshot"],
        }


def passes_volume_filter(
    metrics: dict,
    volume_threshold: float = 1.2,
) -> bool:
    today = metrics["today"]
    return float(today["volume"]) > float(today["volume_sma"]) * volume_threshold


def extract_latest_metrics(
    df: pd.DataFrame,
    ticker: str,
) -> dict:
    engine = LiveSignalEngine(ticker)
    enriched = engine.enrich(df)
    today = BarSnapshot.from_row(enriched.iloc[-1])
    yesterday = BarSnapshot.from_row(enriched.iloc[-2])
    return {
        "today": today.to_metrics_dict(),
        "yesterday": yesterday.to_metrics_dict(),
    }


def derive_position_state(
    df: pd.DataFrame,
    ticker: str,
    end_index: int | None = None,
    external_state: dict[str, Any] | None = None,
) -> dict:
    engine = LiveSignalEngine(ticker)
    enriched = engine.enrich(df)
    state = engine.replay_state(
        enriched,
        end_index=end_index,
        initial_state=external_state,
    )
    return state.to_dict()


def evaluate_live_signal(
    metrics: dict,
    position_state: dict,
    ticker: str,
) -> dict:
    engine = LiveSignalEngine(ticker)

    today = BarSnapshot(
        date=metrics["today"]["date"],
        open=float(metrics["today"].get("open", metrics["today"]["close"])),
        high=float(metrics["today"].get("high", metrics["today"]["close"])),
        low=float(metrics["today"].get("low", metrics["today"]["close"])),
        close=float(metrics["today"]["close"]),
        volume=float(metrics["today"]["volume"]),
        sma_short=float(metrics["today"]["sma_short"]),
        sma_long=float(metrics["today"]["sma_long"]),
        rsi=float(metrics["today"]["rsi"]),
        atr=float(metrics["today"]["atr"]),
        volume_sma=float(metrics["today"]["volume_sma"]),
    )
    yesterday = BarSnapshot(
        date=metrics["yesterday"]["date"],
        open=float(metrics["yesterday"].get("open", metrics["yesterday"]["close"])),
        high=float(metrics["yesterday"].get("high", metrics["yesterday"]["close"])),
        low=float(metrics["yesterday"].get("low", metrics["yesterday"]["close"])),
        close=float(metrics["yesterday"]["close"]),
        volume=float(metrics["yesterday"]["volume"]),
        sma_short=float(metrics["yesterday"]["sma_short"]),
        sma_long=float(metrics["yesterday"]["sma_long"]),
        rsi=float(metrics["yesterday"]["rsi"]),
        atr=float(metrics["yesterday"]["atr"]),
        volume_sma=float(metrics["yesterday"]["volume_sma"]),
    )

    state = engine.load_state(position_state)
    result = engine.evaluate_bar(
        state,
        today,
        yesterday,
        mutate_state=False,
        allow_crossover=True,
    )
    return {k: v for k, v in result.items() if k != "state"}
