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
MAX_TICKER_PARAMETER_LENGTH = 200
MAX_TICKERS_PER_REQUEST = 30
TABLES = {"sp500", "daily", "stocks", "funds", "tickers"}
EXCLUDED_SYMBOLS = {"DISCK", "FOX", "NWS", "TFCFA", "UA"}


class SharadarClient:
    def __init__(self, api_key, session=None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise MarketDataError("Set SHARADAR_API_KEY in private configuration.")
        self._api_key = api_key.strip()
        self._session = requests.Session() if session is None else session

    def table(self, resource, fields, **filters):
        table = resource
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
                        "sort": (
                            "ticker.asc" if table == "tickers" else
                            "date.asc,ticker.asc,action.asc" if table == "sp500" else
                            "date.asc,ticker.asc"
                        ),
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
    """Historical membership/caps/prices with explicit classification/exit policies."""

    def __init__(self, api_key, gics_dir, client=None, *,
                 sector_source="dated_gics", delisting_policy="strict"):
        if sector_source not in {"dated_gics", "sharadar"}:
            raise ValueError("Unsupported sector source.")
        if delisting_policy not in {"strict", "last_close_cash"}:
            raise ValueError("Unsupported delisting policy.")
        self.client = SharadarClient(api_key) if client is None else client
        self.gics_dir = Path(gics_dir)
        self.sector_source = sector_source
        self.delisting_policy = delisting_policy
        self.notes = []
        self.forward_notes = {}
        self._vendor_symbols = {}
        self._metadata = {}

    def _get_metadata(self, tickers):
        for batch, vendor_tickers in self._batches(
            [ticker for ticker in tickers if ticker not in self._metadata]
        ):
            frame = self.client.table(
                "tickers",
                ["ticker", "name", "sector", "industry", "isdelisted", "lastpricedate", "permaticker"],
                ticker=vendor_tickers, table="stocks",
            )
            frame["symbol"] = _symbols(frame)
            if (not frame["symbol"].isin(batch).all()
                    or frame["symbol"].duplicated().any()
                    or set(frame["symbol"]) != set(batch)):
                raise MarketDataError("Sharadar security metadata must cover each requested ticker once.")
            self._metadata.update({row["symbol"]: row for row in frame.to_dict("records")})
        return pd.DataFrame([self._metadata[ticker] for ticker in tickers])

    def get_constituents(self, as_of_date: date):
        self.notes = []
        self.forward_notes = {}
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
        excluded = sorted(set(members) & EXCLUDED_SYMBOLS)
        if excluded:
            self.notes.append(
                "User-requested historical-universe exclusion: " + ", ".join(excluded) +
                ". Results do not represent the complete historical eligible universe."
            )
        self._vendor_symbols.update(members)
        if self.sector_source == "sharadar":
            metadata = self._get_metadata(sorted(members))
            columns = ["name", "sector", "industry", "permaticker"]
            if (metadata[columns].isna().any().any()
                    or metadata[columns].apply(lambda col: col.str.strip().eq("")).any().any()):
                raise MarketDataError("Sharadar security metadata has missing classifications/identifiers.")
            self.notes.append(
                "Historical membership and market caps; sector/industry labels are current "
                "Sharadar classifications, not point-in-time GICS. Classification/look-ahead bias remains."
            )
            return metadata[~metadata["symbol"].isin(EXCLUDED_SYMBOLS)].rename(columns={
                "symbol": "Symbol", "name": "Security", "sector": "GICS Sector",
                "industry": "GICS Sub-Industry", "permaticker": "Security_ID",
            })[["Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "Security_ID"]]
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
        return labels[~labels["Symbol"].isin(EXCLUDED_SYMBOLS)].sort_values("Symbol").reset_index(drop=True)

    def _batches(self, tickers):
        batch, vendor_symbols = [], []
        length = 0
        for ticker in dict.fromkeys(tickers):
            if ticker != "SPY" and ticker not in self._vendor_symbols:
                raise MarketDataError("Load historical constituents before requesting their caps/prices.")
            vendor_symbol = self._vendor_symbols.get(ticker, ticker)
            if len(vendor_symbol) > MAX_TICKER_PARAMETER_LENGTH:
                raise MarketDataError("A security identifier exceeds Sharadar's ticker parameter limit.")
            additional = len(vendor_symbol) + bool(batch)
            if batch and (len(batch) == MAX_TICKERS_PER_REQUEST
                          or length + additional > MAX_TICKER_PARAMETER_LENGTH):
                yield batch, ",".join(vendor_symbols)
                batch, vendor_symbols = [], []
                length = 0
            length += len(vendor_symbol) + bool(batch)
            batch.append(ticker)
            vendor_symbols.append(vendor_symbol)
        if batch:
            yield batch, ",".join(vendor_symbols)

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
        self.forward_notes = {}
        prices = self._daily_closes(tickers, start_date - timedelta(days=7), end_date)
        start_prices = prices.loc[:pd.Timestamp(start_date)]
        if start_prices.empty or start_prices["SPY"].last_valid_index() is None:
            raise MarketDataError("No benchmark close exists on or before the rebalance date.")
        start = start_prices["SPY"].last_valid_index()
        end = prices["SPY"].last_valid_index()
        if start >= end or prices.loc[start, tickers].isna().any():
            raise MarketDataError("Selected holdings lack valid prices at the rebalance boundary.")
        missing = [ticker for ticker in tickers if pd.isna(prices.loc[end, ticker])]
        if missing and self.delisting_policy == "strict":
            self._check_end(prices, tickers)
        result = prices.loc[[start, end], tickers].copy()
        for row in self._get_metadata(missing).to_dict("records") if missing else []:
            ticker = row["symbol"]
            last = prices[ticker].last_valid_index()
            try:
                vendor_last = pd.Timestamp(row["lastpricedate"])
            except (TypeError, ValueError):
                raise MarketDataError(f"{ticker}: invalid delisting metadata date.") from None
            if (str(row["isdelisted"]).lower() not in {"y", "true", "1"}
                    or pd.isna(vendor_last) or last != vendor_last or last <= start):
                raise MarketDataError(
                    f"{ticker}: missing endpoint is not a confirmed terminal delisted price; "
                    "no estimated payout was applied."
                )
            result.loc[end, ticker] = prices.loc[last, ticker]
            self.forward_notes[ticker] = (
                f"ESTIMATE: liquidation at last available adjusted close on {last.date()}, "
                "then cash at 0% until year-end. Actual merger/bankruptcy proceeds unverified; "
                "this can overstate losses recovered and is not a verified delisting return."
            )
        return result

    def get_benchmark_return(self, as_of_date: date):
        from protocol_engine import PRICE_HISTORY_DAYS, prepare_returns

        prices = self.get_weekly_closes(
            ["SPY"], as_of_date - timedelta(days=PRICE_HISTORY_DAYS), as_of_date,
        )
        return float(prepare_returns(prices)[0])
