"""Opt-in historical data from Sharadar's direct API (not Nasdaq Data Link)."""

from datetime import date, timedelta
import io
from pathlib import Path

import pandas as pd
import requests

from data_errors import MarketCapError, MarketDataError, positive_market_cap
from symbol_utils import normalize_symbol


API_ROOT = "https://api.sharadar.com/v1.0/data"
PAGE_SIZE = 10000
TABLES = {"sp500", "daily", "stocks", "funds"}


class SharadarClient:
    def __init__(self, api_key, session=None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise MarketDataError("Set SHARADAR_API_KEY in private configuration.")
        self._api_key = api_key.strip()
        self._session = requests.Session() if session is None else session

    def table(self, table, fields, **filters):
        if table not in TABLES:
            raise ValueError(f"Unsupported Sharadar table: {table}")
        pages = []
        for offset in range(0, 1000000, PAGE_SIZE):
            try:
                response = self._session.get(
                    f"{API_ROOT}/{table}",
                    params={
                        **filters, "api_key": self._api_key, "format": "csv",
                        "fields": ",".join(fields), "limit": PAGE_SIZE, "skip": offset,
                        "sort": "date.asc,ticker.asc,action.asc" if table == "sp500" else "date.asc,ticker.asc",
                    },
                    timeout=(10, 90), allow_redirects=False,
                )
            except requests.RequestException:
                # Request exceptions and provider error bodies can contain the API key.
                raise MarketDataError(
                    f"Sharadar {table} request failed; check connectivity and retry."
                ) from None
            if response.status_code != 200:
                raise MarketDataError(
                    f"Sharadar {table} returned HTTP {response.status_code}; "
                    "check your key, table subscription, and request limits."
                )
            try:
                frame = pd.read_csv(io.StringIO(response.text), dtype=str)
            except (ValueError, pd.errors.ParserError, pd.errors.EmptyDataError):
                raise MarketDataError(f"Sharadar {table} did not return valid CSV.") from None
            if not set(fields).issubset(frame.columns):
                raise MarketDataError(f"Sharadar {table} response is missing required columns.")
            if len(frame) > PAGE_SIZE:
                raise MarketDataError(f"Sharadar {table} exceeded the requested page size.")
            if pages and not frame.empty and frame.equals(pages[-1]):
                raise MarketDataError(f"Sharadar {table} repeated a page; pagination failed.")
            pages.append(frame)
            if len(frame) < PAGE_SIZE:
                return pd.concat(pages, ignore_index=True)
        raise MarketDataError(f"Sharadar {table} exceeded the pagination safety limit.")


def _dates(frame):
    try:
        dates = pd.to_datetime(frame["date"], errors="raise")
    except (ValueError, TypeError):
        raise MarketDataError("Sharadar returned invalid observation dates.") from None
    if dates.isna().any():
        raise MarketDataError("Sharadar returned missing observation dates.")
    return dates


def _symbols(frame):
    symbols = frame["ticker"]
    if symbols.isna().any() or symbols.str.contains(r"\s|,").any() or symbols.eq("").any():
        raise MarketDataError("Sharadar returned invalid ticker identifiers.")
    return symbols.map(normalize_symbol)


class SharadarProvider:
    """Historical membership/caps/prices with externally supplied dated GICS labels.

    No live-data fallback. Early price termination blocks publication until an
    explicit corporate-action/delisting valuation policy is implemented.
    """

    def __init__(self, api_key, gics_dir, client=None):
        self.client = SharadarClient(api_key) if client is None else client
        self.gics_dir = Path(gics_dir)
        self._vendor_symbols = {}

    def get_constituents(self, as_of_date: date):
        history = self.client.table(
            "sp500", ["date", "action", "ticker", "name"],
            **{"from": "1998-01-01", "to": as_of_date.isoformat()},
        )
        history["date"] = _dates(history)
        history["symbol"] = _symbols(history)
        history["action"] = history["action"].str.lower()
        if (history["date"] > pd.Timestamp(as_of_date)).any():
            raise MarketDataError("Sharadar membership includes future observations.")
        if history.duplicated(["date", "action", "symbol"]).any():
            raise MarketDataError("Sharadar membership contains duplicate observations.")
        if not history["action"].isin({"historical", "current", "added", "removed"}).all():
            raise MarketDataError("Sharadar membership contains unsupported actions.")
        snapshots = history[history["action"] == "historical"]
        if snapshots.empty:
            raise MarketDataError("No historical S&P 500 snapshot exists before the selection date.")
        snapshot_date = snapshots["date"].max()
        snapshot = snapshots[snapshots["date"] == snapshot_date]
        if snapshot["symbol"].duplicated().any():
            raise MarketDataError("Sharadar snapshot contains duplicate normalized symbols.")
        members = {row.symbol: row.ticker for row in snapshot.itertuples()}
        changes = history[
            (history["date"] > snapshot_date) & history["action"].isin({"added", "removed"})
        ].sort_values(["date", "action", "symbol"])
        for row in changes.itertuples():
            if row.action == "added":
                if row.symbol in members:
                    raise MarketDataError("Sharadar adds an existing constituent; history is inconsistent.")
                members[row.symbol] = row.ticker
            else:
                if row.symbol not in members:
                    raise MarketDataError("Sharadar removes an unknown constituent; history is inconsistent.")
                del members[row.symbol]
        if not 490 <= len(members) <= 510:
            raise MarketDataError("Historical S&P 500 roster is implausibly small or large; verify coverage.")
        path = self.gics_dir / f"sp500_{as_of_date.isoformat()}.csv"
        try:
            labels = pd.read_csv(path, dtype=str)
        except (OSError, ValueError, pd.errors.ParserError):
            raise MarketDataError(
                f"Provide a dated GICS file at {path}; current vendor metadata is not "
                "a substitute for historical sector classifications."
            ) from None
        required = {"Symbol", "Security", "GICS Sector", "GICS Sub-Industry"}
        if not required.issubset(labels.columns) or labels[list(required)].isna().any().any():
            raise MarketDataError("The dated GICS file has missing columns or labels.")
        labels["Symbol"] = labels["Symbol"].map(normalize_symbol)
        if (labels["Symbol"].duplicated().any()
                or set(labels["Symbol"]) != set(members)
                or labels[list(required)].apply(lambda col: col.str.strip().eq("")).any().any()):
            raise MarketDataError("The dated GICS file must cover the exact historical roster once.")
        self._vendor_symbols.update(members)
        return labels.sort_values("Symbol").reset_index(drop=True)

    def _batches(self, tickers):
        unique = list(dict.fromkeys(tickers))
        for start in range(0, len(unique), 50):
            batch = unique[start:start + 50]
            unknown = [ticker for ticker in batch if ticker != "SPY" and ticker not in self._vendor_symbols]
            if unknown:
                raise MarketDataError("Load historical constituents before requesting their caps/prices.")
            yield batch, ",".join(self._vendor_symbols.get(ticker, ticker) for ticker in batch)

    def get_market_caps(self, tickers, as_of_date: date):
        caps = {}
        start_date = as_of_date - timedelta(days=7)
        for batch, vendor_tickers in self._batches(tickers):
            frame = self.client.table(
                "daily", ["ticker", "date", "marketcap"],
                ticker=vendor_tickers,
                **{"from": start_date.isoformat(), "to": as_of_date.isoformat()},
            )
            frame["date"] = _dates(frame)
            frame["symbol"] = _symbols(frame)
            if (not frame["symbol"].isin(batch).all()
                    or not frame["date"].between(pd.Timestamp(start_date), pd.Timestamp(as_of_date)).all()
                    or frame.duplicated(["date", "symbol"]).any()):
                raise MarketCapError("Sharadar returned unexpected or duplicate market-cap observations.")
            latest = frame.sort_values("date").drop_duplicates("symbol", keep="last")
            values = dict(zip(latest["symbol"], latest["marketcap"]))
            invalid = [ticker for ticker in batch if positive_market_cap(values.get(ticker)) is None]
            if invalid:
                raise MarketCapError(
                    "Historical market caps unavailable for: " + ", ".join(invalid),
                    missing=invalid,
                )
            caps.update({ticker: float(values[ticker]) * 1000000 for ticker in batch})
        return caps

    def _daily_closes(self, tickers, start_date, end_date):
        if start_date > end_date:
            raise ValueError("Price start date must not follow end date.")
        tickers = list(dict.fromkeys([*tickers, "SPY"]))
        frames = []
        for table, group in (
            ("stocks", [ticker for ticker in tickers if ticker != "SPY"]),
            ("funds", ["SPY"]),
        ):
            for batch, vendor_tickers in self._batches(group):
                frame = self.client.table(
                    table, ["ticker", "date", "closeadj"], ticker=vendor_tickers,
                    **{"from": start_date.isoformat(), "to": end_date.isoformat()},
                )
                frame["date"] = _dates(frame)
                frame["symbol"] = _symbols(frame)
                if (not frame["symbol"].isin(batch).all()
                        or not frame["date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date)).all()):
                    raise MarketDataError("Sharadar returned unexpected price observations.")
                frame["closeadj"] = pd.to_numeric(frame["closeadj"], errors="coerce")
                if not frame["closeadj"].map(lambda value: positive_market_cap(value) is not None).all():
                    raise MarketDataError("Sharadar adjusted prices must be finite and positive.")
                frames.append(frame)
        prices = pd.concat(frames, ignore_index=True)
        if prices.duplicated(["date", "symbol"]).any():
            raise MarketDataError("Sharadar returned duplicate security/date prices.")
        missing = set(tickers) - set(prices["symbol"])
        if missing:
            raise MarketDataError("Historical prices unavailable for: " + ", ".join(sorted(missing)))
        result = prices.pivot(index="date", columns="symbol", values="closeadj").sort_index()
        benchmark_end = result["SPY"].last_valid_index()
        if benchmark_end is None or (pd.Timestamp(end_date) - benchmark_end).days > 7:
            raise MarketDataError("SPY coverage ends too early for the requested period.")
        return result

    @staticmethod
    def _check_end(prices, tickers):
        end = prices["SPY"].last_valid_index()
        if end is None:
            raise MarketDataError("SPY prices are unavailable.")
        missing = [ticker for ticker in tickers if pd.isna(prices.loc[end, ticker])]
        if missing:
            raise MarketDataError(
                "Prices end before the benchmark for: " + ", ".join(missing) +
                ". Resolve delisting/corporate-action proceeds or missing quotes; "
                "the last price will not be carried to year-end."
            )
        return end

    def get_weekly_closes(self, tickers, start_date: date, end_date: date):
        prices = self._daily_closes(tickers, start_date, end_date)
        end = self._check_end(prices, tickers)
        prices = prices.loc[:end]
        # Monday labels, like Yahoo weekly bars; partial weeks use only observed days.
        weeks = prices.index.to_period("W-SUN").start_time
        return prices.groupby(weeks).last().reindex(columns=tickers)

    def get_forward_closes(self, tickers, start_date: date, end_date: date):
        prices = self._daily_closes(tickers, start_date - timedelta(days=7), end_date)
        start_prices = prices.loc[:pd.Timestamp(start_date)]
        if start_prices.empty or start_prices["SPY"].last_valid_index() is None:
            raise MarketDataError("No benchmark close exists on or before the rebalance date.")
        start = start_prices["SPY"].last_valid_index()
        end = self._check_end(prices, tickers)
        if start >= end or prices.loc[start, tickers].isna().any():
            raise MarketDataError("Selected holdings lack valid prices at the rebalance boundary.")
        return prices.loc[[start, end], tickers]

    def get_benchmark_return(self, as_of_date: date):
        from protocol_engine import PRICE_HISTORY_DAYS, prepare_returns

        prices = self.get_weekly_closes(
            ["SPY"], as_of_date - timedelta(days=PRICE_HISTORY_DAYS), as_of_date,
        )
        return float(prepare_returns(prices)[0])
