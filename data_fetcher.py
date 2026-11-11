import streamlit as st
import pandas as pd
import yfinance as yf
import requests
import io
import datetime
import concurrent.futures
import logging
import math
import time
from collections import Counter
from functools import wraps

from data_errors import BenchmarkError, MarketCapError, positive_market_cap, summarize_tickers


logger = logging.getLogger(__name__)

MARKET_CAP_MAX_WORKERS = 8
MARKET_CAP_ATTEMPTS = 3
MARKET_CAP_RETRY_DELAY = 1


def retry(max_attempts=3, delay=2):
    """Exponential backoff retry decorator for network calls."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise e
                    time.sleep(delay * (2 ** attempt))
        return wrapper
    return decorator


@st.cache_data(ttl=3600)
@retry(max_attempts=3, delay=2)
def get_benchmark_return(end_date):
    """SPY trailing return; raises BenchmarkError (never returns 0.0) when unavailable."""
    start_date = end_date - datetime.timedelta(days=742)
    # Errors propagate so @retry can retry them and st.cache_data never caches a failure.
    data = yf.download(
        "SPY", start=start_date, end=end_date, interval="1wk", auto_adjust=True, progress=False
    )["Close"]
    data = data.ffill().iloc[-53:].dropna()
    if len(data) < 2:
        raise BenchmarkError(
            f"No SPY weekly closes were returned between {start_date} and {end_date}.")
    # Squeeze to handle any DataFrame/Series dimensionality issues from yfinance updates
    value = float(data.iloc[-1].squeeze() / data.iloc[0].squeeze() - 1.0)
    if not math.isfinite(value):
        raise BenchmarkError(f"SPY trailing return ending {end_date} is not finite.")
    return value


@st.cache_data(ttl=3600)
@retry(max_attempts=3, delay=2)
def fetch_sp500_tickers():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    response = requests.get(url, headers=headers)
    tables = pd.read_html(io.StringIO(response.text))
    df = tables[0]
    return df[["Symbol", "Security", "GICS Sector", "GICS Sub-Industry"]]


class _InvalidMarketCap(ValueError):
    """Retryable: Yahoo returned no usable marketCap for this ticker."""


def _fetch_market_cap(ticker):
    """Return (ticker, positive cap or None, failure description or None)."""
    @retry(max_attempts=MARKET_CAP_ATTEMPTS, delay=MARKET_CAP_RETRY_DELAY)
    def fetch():
        raw = (yf.Ticker(ticker).info or {}).get("marketCap")
        cap = positive_market_cap(raw)
        if cap is None:
            raise _InvalidMarketCap("missing marketCap" if raw is None else "invalid marketCap")
        return cap

    try:
        return ticker, fetch(), None
    except _InvalidMarketCap as exc:
        return ticker, None, str(exc)
    except Exception as exc:
        return ticker, None, type(exc).__name__


def fetch_market_caps(tickers):
    """Attempt every candidate; warn and assign zero for unavailable market caps."""
    tickers = list(tickers)
    if not tickers:
        raise MarketCapError("No tickers requested for market-cap ranking.")
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MARKET_CAP_MAX_WORKERS) as executor:
        futures = [executor.submit(_fetch_market_cap, t) for t in tickers]
        for future in concurrent.futures.as_completed(futures):
            ticker, cap, reason = future.result()
            results[ticker] = (cap, reason)
    mcaps = {t: results[t][0] if results[t][0] is not None else 0.0 for t in tickers}
    failures = {t: results[t][1] for t in tickers if results[t][0] is None}
    if failures:
        reasons = Counter(failures.values())
        detail = "; ".join(f"{reason}: {count}" for reason, count in reasons.most_common())
        logger.warning(
            "Market-cap fetch incomplete: %d/%d valid (%s). Assigned zero and ranked last: %s",
            len(tickers) - len(failures), len(tickers), detail,
            summarize_tickers(sorted(failures)),
        )
    return mcaps


# Zero placeholders are cached too; the sidebar refresh allows another live attempt.
@st.cache_data(ttl=86400, show_spinner=False)
def get_sorted_by_market_cap(tickers):
    mcaps = fetch_market_caps(tickers)
    return sorted(tickers, key=lambda x: mcaps[x], reverse=True), mcaps


def clear_market_data_caches():
    """Drop cached constituents, market caps and benchmark returns (not identity/access state)."""
    for cached in (fetch_sp500_tickers, get_sorted_by_market_cap, get_benchmark_return):
        cached.clear()
