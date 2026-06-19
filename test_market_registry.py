from market_registry import (
    DEFAULT_WATCHLIST,
    TICKER_SECTORS,
    sector_for_ticker,
    validate_watchlist_routing,
)


def test_default_watchlist_has_routing_for_every_ticker() -> None:
    assert validate_watchlist_routing(DEFAULT_WATCHLIST) == []


def test_diversification_tickers_have_non_tech_sectors() -> None:
    diversified = {"LLY", "UNH", "JNJ", "JPM", "V", "XOM", "COST", "WMT", "KO", "CAT"}
    assert diversified.issubset(set(DEFAULT_WATCHLIST))
    tech_sectors = {"mega_tech", "semiconductors", "growth_tech"}
    for ticker in diversified:
        assert sector_for_ticker(ticker) not in tech_sectors
        assert TICKER_SECTORS[ticker] == sector_for_ticker(ticker)
