from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import pandas as pd
import requests

_TICKER_RE = re.compile(r'^[A-Z]{1,5}([.-][A-Z]{1,5})?$')

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
_CACHE_TTL = 86400

_NASDAQ_100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def _read_cache(name: str) -> list[str] | None:
    path = _CACHE_DIR / f"{name}.txt"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > _CACHE_TTL:
        return None
    tickers = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    logger.debug("Cache hit for %s (%d tickers)", name, len(tickers))
    return tickers


def _write_cache(name: str, tickers: list[str]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{name}.txt"
    path.write_text("\n".join(tickers) + "\n")
    logger.debug("Wrote cache for %s (%d tickers)", name, len(tickers))


def _fetch_sp500() -> list[str]:
    cached = _read_cache("sp500")
    if cached is not None:
        return cached

    logger.info("Fetching S&P 500 tickers from Wikipedia")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)"}
    import requests as req
    from io import StringIO
    resp = req.get(_SP500_URL, headers=headers)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    if not tables:
        logger.error("No tables found in S&P 500 Wikipedia page")
        return _read_cache("sp500") or []
    df = tables[0]
    if "Symbol" not in df.columns and len(df.columns) < 2:
        logger.error("S&P 500 table has unexpected columns: %s", list(df.columns))
        return _read_cache("sp500") or []
    col = "Symbol" if "Symbol" in df.columns else df.columns[1]
    tickers = sorted(set(df[col].astype(str).tolist()))
    _write_cache("sp500", tickers)
    return tickers


def _fetch_nasdaq100() -> list[str]:
    cached = _read_cache("nasdaq100")
    if cached is not None:
        return cached

    logger.info("Fetching NASDAQ 100 tickers from Wikipedia")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)"}
    import requests as req
    from io import StringIO
    resp = req.get(_NASDAQ_100_URL, headers=headers)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))
    if not tables or len(tables) < 2:
        logger.error("NASDAQ 100 Wikipedia page has fewer than 2 tables")
        return _read_cache("nasdaq100") or []
    df = tables[1]
    if "Ticker" not in df.columns and "Symbol" not in df.columns and len(df.columns) < 2:
        logger.error("NASDAQ 100 table has unexpected columns: %s", list(df.columns))
        return _read_cache("nasdaq100") or []
    col = "Ticker" if "Ticker" in df.columns else "Symbol" if "Symbol" in df.columns else df.columns[1]
    tickers = sorted(set(df[col].astype(str).tolist()))
    _write_cache("nasdaq100", tickers)
    return tickers


def get_universe() -> list[str]:
    sp500 = _fetch_sp500()
    nasdaq100 = _fetch_nasdaq100()
    merged = sorted(set(sp500) | set(nasdaq100))
    logger.info("Universe: %d unique tickers (SP500=%d, NDX100=%d)", len(merged), len(sp500), len(nasdaq100))
    # Filter out invalid symbols (CUSIPs like 30660-6)
    valid = [t for t in merged if _TICKER_RE.match(t.upper())]
    removed = len(merged) - len(valid)
    if removed:
        logger.warning("Filtered %d invalid symbols from universe", removed)
    logger.info("Universe: %d valid tickers (SP500=%d, NDX100=%d)", len(valid), len(sp500), len(nasdaq100))
    return valid
