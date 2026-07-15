"""Yahoo Finance v8 chart price provider — free, no API key.

Stooq retired its ``/q/l/`` quote endpoint (now 404) and Yahoo's ``/v7/finance/quote``
endpoint returns 401 without a crumb, so we use the unauthenticated ``/v8/finance/chart``
endpoint, which still serves the current price in ``result[0].meta.regularMarketPrice``.

Wire format:
    GET https://query1.finance.yahoo.com/v8/finance/chart/AAPL?interval=1d&range=1d

Returns JSON:
    {"chart": {"result": [{"meta": {"symbol": "AAPL", "currency": "USD",
                                    "regularMarketPrice": 211.16, ...}}],
               "error": null}}

Unknown symbols come back as ``{"chart": {"result": null, "error": {...}}}`` (HTTP 404),
which we treat as "no quote" rather than a hard failure.
"""
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable

import requests

from .base import PriceQuote
from .registry import register

# Yahoo rejects requests without a browser-like User-Agent (429/401).
_USER_AGENT = "Mozilla/5.0 (compatible; FinLab/1.0)"


@register
class YahooChartPriceProvider:
    name = "yahoo_chart"

    def __init__(
        self,
        http: requests.Session | None = None,
        timeout: float = 15.0,
        *,
        max_retries: int = 3,
        backoff: float = 1.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http or requests.Session()
        self._http.headers.setdefault("User-Agent", _USER_AGENT)
        self._timeout = timeout
        self._base = "https://query1.finance.yahoo.com/v8/finance/chart"
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep

    def fetch_quotes(self, symbols: Iterable[str]) -> list[PriceQuote]:
        normalized = [s.strip().upper() for s in symbols if s and s.strip()]
        if not normalized:
            return []

        now = datetime.now(tz=timezone.utc)

        # Sequential, not parallel: Yahoo rate-limits concurrent bursts with HTTP
        # 429, but serves sequential requests fine. Portfolios have a handful of
        # symbols, so the latency is negligible.
        quotes: list[PriceQuote] = []
        for symbol in normalized:
            quote = self._safe_fetch_one(symbol, now)
            if quote is not None:
                quotes.append(quote)
        return quotes

    def _safe_fetch_one(self, symbol: str, at: datetime) -> PriceQuote | None:
        """Never raises — returns the quote on success or None on any failure.

        One bad symbol (404, timeout, malformed JSON) must not sink the batch.
        """
        try:
            return self._fetch_one(symbol, at)
        except Exception:
            return None

    def _fetch_one(self, symbol: str, at: datetime) -> PriceQuote | None:
        url = f"{self._base}/{symbol}"
        response = self._get_with_retry(url)
        if response.status_code == 429:
            return None  # still throttled after retries — drop this symbol
        response.raise_for_status()

        result = ((response.json() or {}).get("chart") or {}).get("result")
        if not result:
            return None
        raw_price = (result[0].get("meta") or {}).get("regularMarketPrice")
        if raw_price is None:
            return None
        try:
            price = Decimal(str(raw_price)).quantize(Decimal("0.0001"))
        except (InvalidOperation, ValueError):
            return None
        return PriceQuote(symbol=symbol, price=price, at=at)

    def _get_with_retry(self, url: str) -> requests.Response:
        """GET with exponential backoff on HTTP 429 (Yahoo's throttle response)."""
        response = None
        for attempt in range(self._max_retries + 1):
            response = self._http.get(
                url, params={"interval": "1d", "range": "1d"}, timeout=self._timeout,
            )
            if response.status_code != 429 or attempt == self._max_retries:
                return response
            self._sleep(self._backoff * (2 ** attempt))
        return response
