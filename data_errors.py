"""Market-data errors and validation helpers; unknown caps may be ranked as zero."""

import math
import re


class MarketDataError(RuntimeError):
    """Required market data is unavailable or invalid; no portfolio may be built."""

    trace_status = "market_data_failed"


class MarketCapError(MarketDataError):
    """Market caps are missing, non-numeric, non-finite or not positive."""

    trace_status = "market_cap_failed"

    def __init__(self, message, missing=(), invalid=(), failures=None):
        super().__init__(message)
        self.missing = list(missing)
        self.invalid = list(invalid)
        self.failures = dict(failures or {})


class BenchmarkError(MarketDataError):
    """The SPY benchmark return or performance-gate threshold is unavailable."""

    trace_status = "benchmark_failed"


def positive_market_cap(value):
    """Return a finite positive float market cap, or None when it is unknown/invalid."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def describe_error(exc, limit=160):
    """Short, credential-safe description: type name plus message without URL queries."""
    message = re.sub(r"\?\S*", "?<redacted>", str(exc)).strip()
    message = re.sub(r"\s+", " ", message)[:limit]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def summarize_tickers(tickers, limit=10):
    tickers = list(tickers)
    shown = ", ".join(tickers[:limit])
    return shown + (f" (+{len(tickers) - limit} more)" if len(tickers) > limit else "")
