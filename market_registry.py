"""US equity watchlist routing and benchmark symbols for KIS OpenAPI."""

from __future__ import annotations

BENCHMARK_TICKER = "SPY"
SECONDARY_BENCHMARK_TICKER = "QQQ"
BENCHMARK_SMA_PERIOD = 200
BENCHMARK_CONFIRM_SMA_PERIOD = 50

# Sector tags for concentration limits (max N positions per sector).
TICKER_SECTORS: dict[str, str] = {
    # Mega-cap / platform tech
    "AAPL": "mega_tech",
    "MSFT": "mega_tech",
    "GOOGL": "mega_tech",
    "AMZN": "mega_tech",
    "META": "mega_tech",
    "NFLX": "mega_tech",
    # Semiconductors
    "NVDA": "semiconductors",
    "AMD": "semiconductors",
    "AVGO": "semiconductors",
    "TSM": "semiconductors",
    # High-beta growth tech
    "TSLA": "growth_tech",
    "PLTR": "growth_tech",
    "CRWD": "growth_tech",
    "SHOP": "growth_tech",
    # Non-tech diversification sleeve
    "LLY": "healthcare",
    "UNH": "healthcare",
    "JNJ": "healthcare",
    "JPM": "financials",
    "V": "financials",
    "XOM": "energy",
    "COST": "consumer_retail",
    "WMT": "consumer_retail",
    "KO": "consumer_staples",
    "CAT": "industrial",
    "UBER": "transport",
    "SPY": "benchmark",
    "QQQ": "benchmark",
}


def sector_for_ticker(ticker: str) -> str:
    return TICKER_SECTORS.get(ticker.upper(), "other")

DEFAULT_WATCHLIST: list[str] = [
    # Tech core (15)
    "AAPL",
    "MSFT",
    "NVDA",
    "META",
    "AMZN",
    "GOOGL",
    "TSLA",
    "AMD",
    "AVGO",
    "NFLX",
    "PLTR",
    "CRWD",
    "TSM",
    "SHOP",
    "UBER",
    # Diversification sleeve (10) — liquid US large-cap, non-tech
    "LLY",
    "UNH",
    "JNJ",
    "JPM",
    "V",
    "XOM",
    "COST",
    "WMT",
    "KO",
    "CAT",
]

# excd / ovrs_excg_cd — VTS overseas routing (NASDAQ default; NYSE for TSM)
MARKET_META: dict[str, dict[str, str]] = {
    "NVDA": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "PLTR": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "AAPL": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "META": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "AMZN": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "MSFT": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "GOOGL": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "TSLA": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "NFLX": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "AMD": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "AVGO": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "CRWD": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "TSM": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "SHOP": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "UBER": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "LLY": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "UNH": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "JNJ": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "JPM": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "V": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "XOM": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "COST": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "WMT": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "KO": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "CAT": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "SPY": {"excd": "AMS", "ovrs_excg_cd": "AMEX"},
    "QQQ": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
}


def parse_watchlist(raw: str | None, fallback: list[str] | None = None) -> list[str]:
    seed = fallback or DEFAULT_WATCHLIST
    if not raw:
        return seed.copy()
    tickers = [token.strip().upper() for token in raw.split(",") if token.strip()]
    return tickers or seed.copy()


def validate_watchlist_routing(tickers: list[str]) -> list[str]:
    """Return tickers missing MARKET_META entries."""
    return [ticker for ticker in tickers if ticker not in MARKET_META]
