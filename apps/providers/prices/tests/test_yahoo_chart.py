from decimal import Decimal
from unittest.mock import MagicMock

from apps.providers.prices.yahoo_chart import YahooChartPriceProvider


def _chart_response(symbol: str, price: str) -> MagicMock:
    r = MagicMock()
    r.json.return_value = {
        "chart": {
            "result": [
                {"meta": {"symbol": symbol.upper(), "currency": "USD",
                          "regularMarketPrice": float(price)}}
            ],
            "error": None,
        }
    }
    r.raise_for_status = MagicMock()
    return r


def _symbol_from_url(url: str) -> str:
    # URL is ".../v8/finance/chart/<SYMBOL>"
    return url.rsplit("/chart/", 1)[1]


def test_fetch_quotes_returns_one_quote_per_symbol():
    http = MagicMock()
    http.get.side_effect = lambda url, params=None, timeout=None: _chart_response(
        _symbol_from_url(url), "211.16"
    )
    provider = YahooChartPriceProvider(http=http)

    quotes = provider.fetch_quotes(["AAPL", "MSFT", "VTI"])

    assert {q.symbol for q in quotes} == {"AAPL", "MSFT", "VTI"}
    assert all(q.price == Decimal("211.1600") for q in quotes)
    assert http.get.call_count == 3


def test_fetch_quotes_isolates_individual_failures():
    """One symbol raising (e.g. HTTP 404) must not kill the batch."""
    def maybe_fail(url, params=None, timeout=None):
        if "FAIL" in url:
            raise RuntimeError("boom")
        return _chart_response(_symbol_from_url(url), "5.00")

    http = MagicMock()
    http.get.side_effect = maybe_fail
    provider = YahooChartPriceProvider(http=http)

    quotes = provider.fetch_quotes(["GOOD1", "FAIL", "GOOD2"])

    assert {q.symbol for q in quotes} == {"GOOD1", "GOOD2"}


def test_fetch_quotes_missing_price_is_skipped():
    """An unknown symbol (result=None) yields no quote rather than crashing."""
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"chart": {"result": None, "error": {"code": "Not Found"}}}
    http = MagicMock()
    http.get.return_value = r
    provider = YahooChartPriceProvider(http=http)

    assert provider.fetch_quotes(["BADSYM"]) == []


def test_fetch_quotes_retries_on_429_then_succeeds():
    """Yahoo throttles bursts with HTTP 429 — a transient 429 should back off
    and retry, not silently drop the symbol."""
    calls = {"n": 0}

    def get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            r = MagicMock()
            r.status_code = 429
            return r
        return _chart_response("AAPL", "100.00")

    http = MagicMock()
    http.get.side_effect = get
    sleeps: list[float] = []
    provider = YahooChartPriceProvider(http=http, sleep=sleeps.append)

    quotes = provider.fetch_quotes(["AAPL"])

    assert [q.price for q in quotes] == [Decimal("100.0000")]
    assert calls["n"] == 3          # two 429s + one success
    assert len(sleeps) == 2         # backed off before each retry


def test_fetch_quotes_gives_up_after_persistent_429():
    """If 429 never clears, the symbol is dropped rather than looping forever."""
    r = MagicMock()
    r.status_code = 429
    http = MagicMock()
    http.get.return_value = r
    provider = YahooChartPriceProvider(http=http, sleep=lambda _s: None)

    assert provider.fetch_quotes(["AAPL"]) == []


def test_fetch_quotes_empty_input_returns_empty_list():
    http = MagicMock()
    provider = YahooChartPriceProvider(http=http)

    assert provider.fetch_quotes([]) == []
    assert provider.fetch_quotes(["", "  "]) == []
    http.get.assert_not_called()
