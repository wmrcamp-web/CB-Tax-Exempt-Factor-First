"""Compare capped 13-factor shadow selection by downside beta vs trailing return."""

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

from compare_factors import ROOT, _PairedProvider, _arm, _check_pair, _full_period
from compare_membership import _write_report
from data_errors import MarketDataError
from sharadar_backtest import configured_provider
from sharadar_provider import EXCLUDED_SYMBOLS


OUTPUT_DIR = ROOT / ".data" / "shadow_priority_comparisons"
UNCHANGED_FILES = ("sharadar_provider.py", "sharadar_backtest.py", "selection_trace.py",
                   "symbol_utils.py", "data_errors.py", "data_provider.py", "metrics.py",
                   "compare_membership.py", "compare_shadow.py")
SOURCE_FILES = (*UNCHANGED_FILES, "compare_shadow_priority.py", "compare_shadow.py",
                "compare_factors.py", "backtest_engine.py", "portfolio_pipeline.py",
                "protocol_engine.py")


def _baseline_by_year(path, provider, years):
    source = Path(path).read_bytes()
    report = json.loads(source)
    if (report.get("factor_count") != 13
            or report.get("shadow_limit") != "at most floor(actual holdings / 4)"
            or not set(years).issubset(report.get("requested_years", []))
            or report.get("sector_source") != provider.sector_source
            or report.get("delisting_policy") != provider.delisting_policy
            or report.get("excluded_symbols") != sorted(EXCLUDED_SYMBOLS)):
        raise MarketDataError("Previous shadow report has different protocol settings.")
    hashes = report.get("source_sha256", {})
    changed = [name for name in UNCHANGED_FILES
               if hashes.get(name) != hashlib.sha256((ROOT / name).read_bytes()).hexdigest()]
    if changed:
        raise MarketDataError("Unchanged source files differ from prior run: " + ", ".join(changed))
    records = {row.get("year"): row for row in report.get("years", [])}
    if any(year not in records or records[year].get("status") != "complete"
           or records[year].get("capped", {}).get("status") != "complete"
           for year in years):
        raise MarketDataError("Previous report lacks completed capped results for every requested year.")
    return source, {year: records[year]["capped"] for year in years}


def _verify_old_priority(current, saved):
    for field in ("constituent_universe", "screened_universe", "market_cap_universe",
                  "price_eligible_tickers", "survivor_tickers", "factor_eligible_tickers",
                  "final_tickers", "benchmark_window_start", "benchmark_window_end", "gate_mode"):
        if current["trace"][field] != saved["trace"][field]:
            raise MarketDataError(f"Existing capped run has changed {field}.")
    for field in ("Portfolio_Return", "Benchmark_Return", "Alpha"):
        if not math.isclose(current["summary"][field], saved["summary"][field],
                            rel_tol=0, abs_tol=1e-9):
            raise MarketDataError(f"Existing capped run has changed {field}.")
    if list(current["daily_values"]) != list(saved["daily_values"]) or any(
        not math.isclose(current["daily_values"][day], saved["daily_values"][day],
                         rel_tol=0, abs_tol=1e-9)
        for day in current["daily_values"]
    ):
        raise MarketDataError("Existing capped run has different daily portfolio values.")
    current_holdings = sorted(current["holdings"], key=lambda row: row["Ticker"])
    saved_holdings = sorted(saved["holdings"], key=lambda row: row["Ticker"])
    if len(current_holdings) != len(saved_holdings):
        raise MarketDataError("Existing capped run has a different holdings count.")
    for actual, expected in zip(current_holdings, saved_holdings):
        for field in ("Ticker", "In_Shadow_List"):
            if actual.get(field) != expected.get(field):
                raise MarketDataError(f"Existing capped run has changed {field}.")
        for field in ("Weight", "Annual_Return", "Start_Price", "End_Price"):
            try:
                equal = math.isclose(float(actual[field]), float(expected[field]),
                                     rel_tol=0, abs_tol=1e-9)
            except (KeyError, TypeError, ValueError):
                raise MarketDataError(f"Existing capped run is missing valid {field} values.") from None
            if not equal:
                raise MarketDataError(f"Existing capped run has changed {field}.")


def run_comparison(provider, years, baseline_path, output_path, progress=print):
    years = list(years)
    if (not years or years != list(range(years[0], years[-1] + 1))
            or years[0] < 1999 or years[-1] >= date.today().year):
        raise ValueError("Choose consecutive completed years from 1999 onward.")
    baseline_bytes, baseline = _baseline_by_year(baseline_path, provider, years)
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_report_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "requested_years": years,
        "factor_count": 13,
        "shadow_limit": "at most floor(actual holdings / 4)",
        "baseline_shadow_priority": "lowest downside beta first",
        "alternative_shadow_priority": "highest trailing return first; ties by lowest downside beta then ticker",
        "sector_source": provider.sector_source,
        "delisting_policy": provider.delisting_policy,
        "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in SOURCE_FILES},
        "limitations": [
            "Exploratory in-sample comparison; selecting the winner risks overfitting.",
            "Both sides use current sector classifications and the same user-requested exclusions.",
            "Last-close cash exit estimates are not verified merger or delisting proceeds.",
            "Daily-close drawdowns use buy-and-hold positions with annual rebalancing, not intraday prices.",
            "Annualized excess is portfolio CAGR minus SPY CAGR, not regression alpha.",
            "The same 25% shadow cap may result in smaller-than-target portfolios.",
        ],
        "years": [],
    }
    _write_report(output_path, report)
    for year in years:
        progress(f"\nComparing {year}...")
        paired = _PairedProvider(provider)
        beta_first = _arm(paired, year, 13, shadow_fraction_limit=0.25)
        return_first = _arm(paired, year, 13, shadow_fraction_limit=0.25,
                            shadow_priority="trailing_return")
        pair = {"year": year, "downside_beta_priority": beta_first,
                "trailing_return_priority": return_first, "status": "failed"}
        if beta_first["status"] == return_first["status"] == "complete":
            try:
                _check_pair(beta_first, return_first)
                if (beta_first["trace"]["factor_eligible_tickers"]
                        != return_first["trace"]["factor_eligible_tickers"]):
                    raise MarketDataError("Factor-eligible universes differ between priorities.")
                _verify_old_priority(beta_first, baseline[year])
                for name, arm in (("downside-beta", beta_first), ("trailing-return", return_first)):
                    shadow_count = sum(row["In_Shadow_List"] for row in arm["holdings"])
                    if 4 * shadow_count > len(arm["holdings"]):
                        raise MarketDataError(f"{name} priority exceeds the 25% shadow cap.")
            except MarketDataError as exc:
                pair["error"] = str(exc)
            else:
                pair["status"] = "complete"
                pair["return_difference"] = (
                    return_first["summary"]["Portfolio_Return"]
                    - beta_first["summary"]["Portfolio_Return"]
                )
                pair["trailing_return_shadow_count"] = sum(
                    row["In_Shadow_List"] for row in return_first["holdings"]
                )
                progress(
                    f"{year}: downside-beta priority {beta_first['summary']['Portfolio_Return']:.2%} "
                    f"(alpha {beta_first['summary']['Alpha']:+.2%}, "
                    f"drawdown {beta_first['max_drawdown']:.2%}); trailing-return priority "
                    f"{return_first['summary']['Portfolio_Return']:.2%} "
                    f"(alpha {return_first['summary']['Alpha']:+.2%}, "
                    f"drawdown {return_first['max_drawdown']:.2%}); "
                    f"difference {pair['return_difference']:+.2%}."
                )
        if pair["status"] == "failed":
            errors = [pair.get("error", "")] + [
                f"{name}: {arm.get('error', arm['trace']['backtest_status'])}"
                for name, arm in (("downside-beta priority", beta_first),
                                  ("trailing-return priority", return_first))
                if arm["status"] != "complete"
            ]
            progress(f"{year}: incomplete; " + "; ".join(filter(None, errors)))
        report["years"].append(pair)
        _write_report(output_path, report)
    complete = all(row["status"] == "complete" for row in report["years"])
    if complete:
        for earlier, later in zip(report["years"], report["years"][1:]):
            for name in ("downside_beta_priority", "trailing_return_priority"):
                if list(earlier[name]["daily_values"])[-1] != list(later[name]["daily_values"])[0]:
                    report["full_period_error"] = (
                        f"Daily paths lack a shared rebalance boundary between "
                        f"{earlier['year']} and {later['year']}; full-period statistics withheld."
                    )
                    progress(report["full_period_error"])
                    complete = False
                    break
            if not complete:
                break
    if complete:
        report["full_period"] = {
            name: _full_period(report["years"], name)
            for name in ("downside_beta_priority", "trailing_return_priority")
        }
        for name, stats in report["full_period"].items():
            progress(f"{name}: annualized {stats['annualized_return']:.2%}; "
                     f"annualized excess vs SPY {stats['annualized_excess_return']:+.2%}; "
                     f"daily-close maximum drawdown {stats['max_drawdown']:.2%}.")
    _write_report(output_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--start-year", type=int, default=2016)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--estimate-delisted", action="store_true")
    args = parser.parse_args()
    if not 1999 <= args.start_year <= args.end_year < date.today().year:
        parser.error("Choose consecutive completed years from 1999 onward.")
    path = OUTPUT_DIR / f"comparison_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}.json"
    try:
        provider = configured_provider(delisting_policy="last_close_cash" if args.estimate_delisted else "strict")
        report = run_comparison(provider, range(args.start_year, args.end_year + 1),
                                args.baseline_report, path)
        print(f"\nPrivate comparison report: {path}")
        failures = [row["year"] for row in report["years"] if row["status"] != "complete"]
        if failures:
            parser.exit(1, "Incomplete years: " + ", ".join(map(str, failures)) + "\n")
        if "full_period_error" in report:
            parser.exit(1, report["full_period_error"] + "\n")
    except (MarketDataError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Comparison failed: {exc}\n")


if __name__ == "__main__":
    main()
