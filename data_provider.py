"""Date-aware boundary for portfolio and backtest data acquisition."""

from datetime import date
from typing import Mapping, Protocol, Sequence

import pandas as pd
import data_fetcher
import yfinance as yf


class DataProvider(Protocol):
    def get_constituents(self, as_of_date: date) -> pd.DataFrame:
        ...

    def get_market_caps(self, tickers: Sequence[str], as_of_date: date) -> Mapping[str, object]:
        ...

    def get_weekly_closes(
        self, tickers: Sequence[str], start_date: date, end_date: date
    ) -> pd.DataFrame:
        ...

    def get_benchmark_return(self, as_of_date: date) -> float:
        ...


class LiveProvider:
    """Current Wikipedia and Yahoo Finance data, regardless of the supplied date."""

    def get_constituents(self, as_of_date: date) -> pd.DataFrame:
        return data_fetcher.fetch_sp500_tickers()

    def get_market_caps(self, tickers: Sequence[str], as_of_date: date) -> Mapping[str, object]:
        _, market_caps = data_fetcher.get_sorted_by_market_cap(tickers)
        return market_caps

    def get_weekly_closes(
        self, tickers: Sequence[str], start_date: date, end_date: date
    ) -> pd.DataFrame:
        return yf.download(
            tickers, start=start_date, end=end_date, interval="1wk", auto_adjust=True,
            progress=False
        )["Close"]

    def get_benchmark_return(self, as_of_date: date) -> float:
        return data_fetcher.get_benchmark_return(as_of_date)


DEFAULT_PROVIDER = LiveProvider()
