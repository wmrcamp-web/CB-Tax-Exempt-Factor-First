"""Compare eight and thirteen factors using historical Sharadar membership."""

import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
from uuid import uuid4

import pandas as pd

from backtest_engine import compute_backtest_year
from compare_membership import _compatible, _write_report
from data_errors import MarketDataError
from sharadar_backtest import configured_provider
from sharadar_provider import EXCLUDED_SYMBOLS


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / ".data" / "factor_comparisons"
FACTORS = (8, 13)


class _PairedProvider:
    """Freeze selection inputs for the two factor fits in one year."""

    def __init__(self, base):
        self.base = base
        self.constituents = None
        self.selection_notes = None
        self.caps = {}
        self.weekly = {}

    def __getattr__(self, name):
        return getattr(self.base, name)

    def get_constituents(self, as_of_date):
        if self.constituents is None:
            self.constituents = self.base.get_constituents(as_of_date).copy()
            self.selection_notes = list(self.base.notes)
        self.base.notes = list(self.selection_notes)
        self.base.forward_notes = {}
        return self.constituents.copy()

    def get_market_caps(self, tickers, as_of_date):
        key = (tuple(tickers), as_of_date)
        if key not in self.caps:
            self.caps[key] = dict(self.base.get_market_caps(tickers, as_of_date))
        return dict(self.caps[key])

    def get_weekly_closes(self, tickers, start_date, end_date):
        key = (tuple(tickers), start_date, end_date)
        if key not in self.weekly:
            self.weekly[key] = self.base.get_weekly_closes(tickers, start_date, end_date).copy()
        return self.weekly[key].copy()


def _daily_values(provider, year, result):
    """Value equal-weight buy-and-hold positions at each benchmark close."""
    start_date, end_date = date(year - 1, 12, 31), date(year, 12, 31)
    holdings = result.details
    tickers = [row["Ticker"] for row in holdings]
    prices = provider._daily_closes(tickers, start_date - timedelta(days=7), end_date)
    benchmark = prices["SPY"].dropna()
    before = benchmark.loc[:pd.Timestamp(start_date)]
    if before.empty:
        raise MarketDataError("No benchmark close on or before the rebalance date.")
    start, end = before.index[-1], benchmark.index[-1]
    prices = prices.reindex(benchmark.loc[start:end].index)
    for row in holdings:
        ticker = row["Ticker"]
        series = prices[ticker]
        if row.get("Exit_Valuation", "").startswith("ESTIMATE:"):
            last = series.last_valid_index()
            if last is None:
                raise MarketDataError(f"{ticker}: no observed price for estimated exit.")
            prices.loc[prices.index > last, ticker] = series.loc[last]
        if prices[ticker].isna().any():
            raise MarketDataError(
                f"{ticker}: missing daily close before its endpoint; drawdown cannot be verified."
            )
        if not (math.isclose(float(prices[ticker].iloc[0]), float(row["Start_Price"]),
                             rel_tol=1e-10, abs_tol=1e-10)
                and math.isclose(float(prices[ticker].iloc[-1]), float(row["End_Price"]),
                                 rel_tol=1e-10, abs_tol=1e-10)):
            raise MarketDataError(f"{ticker}: daily endpoints differ from the backtest.")
    values = sum(
        row["Weight"] * prices[row["Ticker"]] / prices[row["Ticker"]].iloc[0]
        for row in holdings
    )
    if not math.isclose(float(values.iloc[-1]) - 1, result.summary["Portfolio_Return"],
                        rel_tol=0, abs_tol=1e-9):
        raise MarketDataError("Daily values do not reconcile to the annual portfolio return.")
    return {index.date().isoformat(): float(value) for index, value in values.items()}


def _max_drawdown(values):
    peak = 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def _arm(provider, year, factors, *, shadow_fraction_limit=None):
    traces = []
    options = ({"shadow_fraction_limit": shadow_fraction_limit}
               if shadow_fraction_limit is not None else {})
    result = compute_backtest_year(year, provider=provider, traces=traces, n_factors=factors, **options)
    if not traces:
        raise MarketDataError("No selection trace was produced.")
    trace = traces[-1]
    arm = {
        "status": "failed",
        "summary": result.summary if result else None,
        "holdings": result.details if result else [],
        "trace": asdict(trace),
    }
    if result is None:
        return arm
    if len(trace.price_eligible_tickers) < factors:
        arm["error"] = f"Fewer than {factors} eligible stocks; requested factor count was not fitted."
        return arm
    try:
        arm["daily_values"] = _daily_values(provider, year, result)
    except MarketDataError as exc:
        arm["error"] = str(exc)
        return arm
    arm["max_drawdown"] = _max_drawdown(arm["daily_values"].values())
    arm["status"] = "complete"
    return arm


def _check_pair(a, b):
    _compatible(a, b)
    for field in ("constituent_universe", "screened_universe", "market_cap_universe",
                  "price_eligible_tickers", "survivor_tickers"):
        if a["trace"][field] != b["trace"][field]:
            raise MarketDataError(f"Paired runs have different {field}; comparison withheld.")
    if list(a["daily_values"]) != list(b["daily_values"]):
        raise MarketDataError("Daily benchmark calendars differ between paired runs; comparison withheld.")


def _full_period(years, arm_name):
    wealth = 1.0
    path = [wealth]
    benchmark_wealth = 1.0
    for year in years:
        arm = year[arm_name]
        path.extend(wealth * value for value in list(arm["daily_values"].values())[1:])
        wealth *= 1 + arm["summary"]["Portfolio_Return"]
        benchmark_wealth *= 1 + arm["summary"]["Benchmark_Return"]
    annualized = wealth ** (1 / len(years)) - 1
    benchmark_annualized = benchmark_wealth ** (1 / len(years)) - 1
    return {
        "annualized_return": annualized,
        "annualized_benchmark_return": benchmark_annualized,
        "annualized_excess_return": annualized - benchmark_annualized,
        "max_drawdown": _max_drawdown(path),
    }


def run_comparison(provider, years, output_path, progress=print):
    years = list(years)
    if (not years or years != list(range(years[0], years[-1] + 1))
            or years[0] < 1999 or years[-1] >= date.today().year):
        raise ValueError("Choose consecutive completed years from 1999 onward.")
    if provider.sector_source != "sharadar":
        raise MarketDataError("This comparison requires the same Sharadar sector source as the prior run.")
    source_files = ("compare_factors.py", "compare_membership.py", "sharadar_backtest.py",
                    "sharadar_provider.py", "backtest_engine.py", "portfolio_pipeline.py",
                    "protocol_engine.py", "selection_trace.py", "symbol_utils.py",
                    "data_errors.py", "data_provider.py", "metrics.py")
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "requested_years": years,
        "factor_counts": list(FACTORS),
        "sector_source": provider.sector_source,
        "delisting_policy": provider.delisting_policy,
        "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in source_files},
        "limitations": [
            "Exploratory in-sample comparison; selecting the winner risks overfitting.",
            "Historical membership and caps use Sharadar; sector classifications are current.",
            "Confirmed delisting estimates use last adjusted close and then cash, not verified proceeds.",
            "Drawdowns use daily closing values of equal-weight buy-and-hold positions, with annual rebalancing; "
            "they are not intraday drawdowns and exclude fees and taxes.",
            "Annualized excess return is portfolio CAGR minus benchmark CAGR, not regression alpha.",
        ],
        "years": [],
    }
    _write_report(output_path, report)
    for year in years:
        progress(f"\nComparing {year}...")
        paired_provider = _PairedProvider(provider)
        a = _arm(paired_provider, year, 8)
        b = _arm(paired_provider, year, 13)
        pair = {"year": year, "8_factors": a, "13_factors": b, "status": "failed"}
        if a["status"] == b["status"] == "complete":
            try:
                _check_pair(a, b)
            except MarketDataError as exc:
                pair["error"] = str(exc)
            else:
                pair["status"] = "complete"
                pair["return_difference"] = b["summary"]["Portfolio_Return"] - a["summary"]["Portfolio_Return"]
                progress(
                    f"{year}: 8 factors {a['summary']['Portfolio_Return']:.2%} "
                    f"(alpha {a['summary']['Alpha']:+.2%}, drawdown {a['max_drawdown']:.2%}); "
                    f"13 factors {b['summary']['Portfolio_Return']:.2%} "
                    f"(alpha {b['summary']['Alpha']:+.2%}, drawdown {b['max_drawdown']:.2%}); "
                    f"difference {pair['return_difference']:+.2%}."
                )
        if pair["status"] == "failed":
            progress(f"{year}: incomplete; " + "; ".join(
                [pair.get("error", "")] + [
                    f"{name}: {arm.get('error', arm['trace']['backtest_status'])}"
                    for name, arm in (("8 factors", a), ("13 factors", b))
                    if arm["status"] != "complete"
                ]).strip("; "))
        report["years"].append(pair)
        _write_report(output_path, report)
    complete = all(pair["status"] == "complete" for pair in report["years"])
    if complete:
        for earlier, later in zip(report["years"], report["years"][1:]):
            for name in ("8_factors", "13_factors"):
                if list(earlier[name]["daily_values"])[-1] != list(later[name]["daily_values"])[0]:
                    report["full_period_error"] = (
                        f"Daily paths do not share a rebalance boundary between "
                        f"{earlier['year']} and {later['year']}; full-period statistics withheld."
                    )
                    progress(report["full_period_error"])
                    complete = False
                    break
            if not complete:
                break
    if complete:
        report["full_period"] = {
            name: _full_period(report["years"], name) for name in ("8_factors", "13_factors")
        }
        for name, stats in report["full_period"].items():
            progress(f"{name}: annualized {stats['annualized_return']:.2%}; "
                     f"annualized excess vs SPY {stats['annualized_excess_return']:+.2%}; "
                     f"daily-close maximum drawdown {stats['max_drawdown']:.2%}.")
    _write_report(output_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2016)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--estimate-delisted", action="store_true",
                        help="Use last-close cash estimates for confirmed delistings, not verified proceeds.")
    args = parser.parse_args()
    if not 1999 <= args.start_year <= args.end_year < date.today().year:
        parser.error("Choose completed years from 1999 onward.")
    path = OUTPUT_DIR / f"comparison_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}.json"
    try:
        provider = configured_provider(delisting_policy="last_close_cash" if args.estimate_delisted else "strict")
        report = run_comparison(provider, range(args.start_year, args.end_year + 1), path)
        print(f"\nPrivate comparison report: {path}")
        failures = [item["year"] for item in report["years"] if item["status"] != "complete"]
        if failures:
            parser.exit(1, "Incomplete years: " + ", ".join(map(str, failures)) + "\n")
        if "full_period_error" in report:
            parser.exit(1, report["full_period_error"] + "\n")
    except (MarketDataError, OSError, ValueError) as exc:
        parser.exit(1, f"Comparison failed: {exc}\n")


if __name__ == "__main__":
    main()
