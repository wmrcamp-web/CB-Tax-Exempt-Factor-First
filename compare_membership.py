"""Paired historical-versus-current-membership Sharadar backtest."""

import argparse
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from uuid import uuid4

import pandas as pd

from backtest_engine import compute_backtest_year
from data_errors import MarketDataError
from sharadar_backtest import configured_provider
from sharadar_provider import EXCLUDED_SYMBOLS, _dates, _symbols


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / ".data" / "membership_comparisons"


class CurrentMembershipProvider:
    """Same Sharadar provider, replacing only the membership snapshot."""

    def __init__(self, base):
        if base.sector_source != "sharadar":
            raise MarketDataError("Paired comparison requires Sharadar sector metadata on both sides.")
        self.base = base
        self.members = None
        self.snapshot_date = None
        self.first_prices = {}

    def __getattr__(self, name):
        return getattr(self.base, name)

    def _load_members(self):
        frame = self.base.client.table(
            "sp500", ["date", "action", "ticker", "name"], action="current",
            **{"from": "1998-01-01", "to": date.today().isoformat()},
        )
        frame["date"] = _dates(frame)
        frame["symbol"] = _symbols(frame)
        if (frame.empty or not frame["action"].str.lower().eq("current").all()
                or frame["date"].nunique() != 1
                or frame["date"].iloc[0].date() > date.today()
                or frame["symbol"].duplicated().any()
                or not 490 <= len(frame) <= 510):
            raise MarketDataError("Current S&P 500 snapshot is missing, inconsistent, or incomplete.")
        self.snapshot_date = frame["date"].iloc[0].date().isoformat()
        self.members = dict(zip(frame["symbol"], frame["ticker"]))
        self.base._vendor_symbols.update(self.members)
        metadata = self.base._get_metadata(sorted(self.members))
        cols = ["name", "sector", "industry", "permaticker"]
        if (metadata[cols].isna().any().any()
                or metadata[cols].apply(lambda col: col.str.strip().eq("")).any().any()):
            raise MarketDataError("Current constituent metadata has missing classifications or identifiers.")
        for batch, tickers in self.base._batches(sorted(self.members)):
            listing = self.base.client.table(
                "tickers", ["ticker", "firstpricedate"], ticker=tickers, table="stocks",
            )
            listing["symbol"] = _symbols(listing)
            if (listing["symbol"].duplicated().any()
                    or set(listing["symbol"]) != set(batch)):
                raise MarketDataError("Current constituent first-price dates are incomplete or duplicated.")
            try:
                listing["firstpricedate"] = pd.to_datetime(listing["firstpricedate"], errors="raise")
            except (TypeError, ValueError):
                raise MarketDataError("Invalid first-price date in Sharadar security metadata.") from None
            if listing["firstpricedate"].isna().any():
                raise MarketDataError("Missing first-price date in Sharadar security metadata.")
            self.first_prices.update(dict(zip(listing["symbol"], listing["firstpricedate"])))
        self.metadata = metadata

    def get_constituents(self, as_of_date):
        if self.members is None:
            self._load_members()
        if as_of_date >= date.fromisoformat(self.snapshot_date):
            raise MarketDataError("Comparison selection must precede the frozen current-membership snapshot.")
        self.base._vendor_symbols.update(self.members)
        not_yet_priced = sorted(
            symbol for symbol, first in self.first_prices.items()
            if first > pd.Timestamp(as_of_date)
        )
        exclude = set(not_yet_priced) | EXCLUDED_SYMBOLS
        self.base.notes = [
            f"Deliberately biased current membership snapshot dated {self.snapshot_date}.",
            f"Excluded {len(not_yet_priced)} securities first priced after {as_of_date}: "
            + ", ".join(not_yet_priced),
            "User-requested exclusions: " + ", ".join(sorted(set(self.members) & EXCLUDED_SYMBOLS)),
            "Sector/industry labels are current Sharadar classifications, not point-in-time GICS.",
        ]
        self.base.forward_notes = {}
        return self.metadata[~self.metadata["symbol"].isin(exclude)].rename(columns={
            "symbol": "Symbol", "name": "Security", "sector": "GICS Sector",
            "industry": "GICS Sub-Industry", "permaticker": "Security_ID",
        })[["Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "Security_ID"]].copy()


def _result(year, provider):
    traces = []
    result = compute_backtest_year(year, provider=provider, traces=traces)
    if not traces:
        raise MarketDataError("No selection trace was produced.")
    trace = traces[-1]
    return {
        "status": "complete" if result is not None else "failed",
        "summary": result.summary if result else None,
        "holdings": result.details if result else [],
        "trace": asdict(trace),
    }


def _compatible(historical, current):
    a, b = historical["trace"], current["trace"]
    if (a["gate_mode"] != "benchmark" or b["gate_mode"] != "benchmark"
            or a["benchmark_window_start"] != b["benchmark_window_start"]
            or a["benchmark_window_end"] != b["benchmark_window_end"]
            or a["gate_threshold"] is None or b["gate_threshold"] is None
            or not math.isclose(a["gate_threshold"], b["gate_threshold"], rel_tol=0, abs_tol=1e-10)
            or not math.isclose(historical["summary"]["Benchmark_Return"],
                                current["summary"]["Benchmark_Return"], rel_tol=0, abs_tol=1e-10)):
        raise MarketDataError("Benchmark windows or returns differ between paired runs; comparison withheld.")


def _write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp",
        ) as temp:
            temp_path = Path(temp.name)
            json.dump(report, temp, indent=2, allow_nan=False)
            temp.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def run_comparison(provider, years, output_path, progress=print):
    years = list(years)
    if not years or any(year < 1999 or year >= date.today().year for year in years):
        raise ValueError("Choose completed years from 1999 onward.")
    biased = CurrentMembershipProvider(provider)
    biased._load_members()
    files = ("compare_membership.py", "sharadar_backtest.py", "sharadar_provider.py",
             "backtest_engine.py", "portfolio_pipeline.py", "protocol_engine.py",
             "selection_trace.py", "symbol_utils.py", "data_errors.py", "data_provider.py",
             "metrics.py")
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "membership_snapshot_date": biased.snapshot_date,
        "current_members": biased.members,
        "current_member_first_price_dates": {
            symbol: value.date().isoformat() for symbol, value in biased.first_prices.items()
        },
        "current_member_security_ids": dict(zip(
            biased.metadata["symbol"], biased.metadata["permaticker"],
        )),
        "sector_source": provider.sector_source,
        "delisting_policy": provider.delisting_policy,
        "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files},
        "limitations": [
            "Only membership is intentionally changed; all other provider and protocol settings are shared.",
            "Current membership is future information; securities first priced after selection are excluded. "
            "First-price date is vendor coverage, not proof of initial listing.",
            "Current sector classifications are not point-in-time GICS; identity and price coverage need audit.",
            "Last-close cash exit estimates are not verified merger or delisting proceeds.",
            "Returns are not investment advice; missing years make cumulative comparisons incomplete.",
        ],
        "requested_years": years,
        "years": [],
    }
    _write_report(output_path, report)
    for year in years:
        progress(f"\nComparing {year}...")
        a = _result(year, provider)
        b = _result(year, biased)
        pair = {"year": year, "historical_membership": a, "current_membership": b}
        if a["status"] == b["status"] == "complete":
            try:
                _compatible(a, b)
            except MarketDataError as exc:
                pair["status"] = "failed"
                pair["error"] = str(exc)
                progress(f"{year}: {exc}")
            else:
                pair["status"] = "complete"
                pair["return_difference"] = (
                    b["summary"]["Portfolio_Return"] - a["summary"]["Portfolio_Return"]
                )
                progress(f"{year}: historical {a['summary']['Portfolio_Return']:.2%}; "
                         f"current-membership {b['summary']['Portfolio_Return']:.2%}; "
                         f"difference {pair['return_difference']:+.2%}.")
        else:
            pair["status"] = "failed"
            progress(f"{year}: incomplete; see per-side trace in report.")
        report["years"].append(pair)
        _write_report(output_path, report)
    if (years == list(range(years[0], years[-1] + 1))
            and all(item["status"] == "complete" for item in report["years"])):
        a = math.prod(1 + item["historical_membership"]["summary"]["Portfolio_Return"]
                      for item in report["years"])
        b = math.prod(1 + item["current_membership"]["summary"]["Portfolio_Return"]
                      for item in report["years"])
        report["full_period"] = {
            "historical_annualized_return": a ** (1 / len(years)) - 1,
            "current_membership_annualized_return": b ** (1 / len(years)) - 1,
        }
        progress(f"\nAnnualized: historical {report['full_period']['historical_annualized_return']:.2%}; "
                 f"current-membership {report['full_period']['current_membership_annualized_return']:.2%}.")
        _write_report(output_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2016)
    parser.add_argument("--end-year", type=int, default=date.today().year - 1)
    parser.add_argument(
        "--estimate-delisted", action="store_true",
        help="Estimate confirmed delisting exits at the last adjusted close, then 0%% cash; "
             "actual proceeds are unverified.",
    )
    args = parser.parse_args()
    if not 1999 <= args.start_year <= args.end_year < date.today().year:
        parser.error("Choose completed portfolio years from 1999 onward.")
    path = OUTPUT_DIR / f"comparison_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}.json"
    try:
        provider = configured_provider(
            delisting_policy="last_close_cash" if args.estimate_delisted else "strict",
        )
        report = run_comparison(provider, range(args.start_year, args.end_year + 1), path)
        print(f"\nPrivate comparison report: {path}")
        failures = [item["year"] for item in report["years"] if item["status"] != "complete"]
        if failures:
            parser.exit(1, "Incomplete years: " + ", ".join(map(str, failures)) + "\n")
    except (MarketDataError, OSError, ValueError) as exc:
        parser.exit(1, f"Comparison failed: {exc}\n")


if __name__ == "__main__":
    main()
