"""Compare 13 factors with and without a 25% shadow-holdings cap."""

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


OUTPUT_DIR = ROOT / ".data" / "shadow_comparisons"
UNCHANGED_FILES = ("sharadar_provider.py", "sharadar_backtest.py", "selection_trace.py",
                   "symbol_utils.py", "data_errors.py", "data_provider.py", "metrics.py",
                   "compare_membership.py")
SOURCE_FILES = (*UNCHANGED_FILES, "compare_shadow.py", "compare_factors.py",
                "backtest_engine.py", "portfolio_pipeline.py", "protocol_engine.py")


def _verify_baseline(baseline, provider, years):
    if (not set(years).issubset(baseline.get("requested_years", []))
            or baseline.get("factor_counts") != [8, 13]
            or baseline.get("sector_source") != provider.sector_source
            or baseline.get("delisting_policy") != provider.delisting_policy
            or baseline.get("excluded_symbols") != sorted(EXCLUDED_SYMBOLS)):
        raise MarketDataError("Previous factor report has different years or settings.")
    hashes = baseline.get("source_sha256", {})
    changed = [name for name in UNCHANGED_FILES
               if hashes.get(name) != hashlib.sha256((ROOT / name).read_bytes()).hexdigest()]
    if changed:
        raise MarketDataError("Files differ from previous factor report: " + ", ".join(changed))
    records = baseline.get("years", [])
    by_year = {row.get("year"): row for row in records}
    if len(by_year) != len(records) or any(
        year not in by_year or by_year[year].get("status") != "complete"
        or by_year[year].get("13_factors", {}).get("status") != "complete"
        for year in years
    ):
        raise MarketDataError("Previous report does not contain all completed 13-factor years.")
    return {year: by_year[year]["13_factors"] for year in years}


def _verify_previous(current, previous):
    if current["trace"]["final_tickers"] != previous["trace"]["final_tickers"]:
        raise MarketDataError("Recomputed 13-factor holdings differ from the previous report.")
    for field in ("constituent_universe", "screened_universe", "market_cap_universe",
                  "price_eligible_tickers", "survivor_tickers", "factor_eligible_tickers",
                  "benchmark_window_start", "benchmark_window_end", "gate_mode"):
        if current["trace"][field] != previous["trace"][field]:
            raise MarketDataError(f"Recomputed {field} differs from the previous report.")
    for field in ("Portfolio_Return", "Benchmark_Return", "Alpha"):
        if not math.isclose(current["summary"][field], previous["summary"][field],
                            rel_tol=0, abs_tol=1e-9):
            raise MarketDataError(f"Recomputed {field} differs from the previous report.")
    old, new = previous["daily_values"], current["daily_values"]
    if list(old) != list(new) or any(
        not math.isclose(old[day], new[day], rel_tol=0, abs_tol=1e-9) for day in old
    ):
        raise MarketDataError("Recomputed daily values differ from the previous report.")


def run_comparison(provider, years, baseline_path, output_path, progress=print):
    years = list(years)
    if (not years or years != list(range(years[0], years[-1] + 1))
            or years[0] < 1999 or years[-1] >= date.today().year):
        raise ValueError("Choose consecutive completed years from 1999 onward.")
    baseline_bytes = Path(baseline_path).read_bytes()
    baseline = json.loads(baseline_bytes)
    previous = _verify_baseline(baseline, provider, years)
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "previous_report_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "requested_years": years,
        "factor_count": 13,
        "shadow_limit": "at most floor(actual holdings / 4)",
        "sector_source": provider.sector_source,
        "delisting_policy": provider.delisting_policy,
        "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in SOURCE_FILES},
        "limitations": [
            "Exploratory in-sample comparison; selecting the winner risks overfitting.",
            "Current sector classifications are not point-in-time; excluded securities remain excluded.",
            "Last-close cash exit estimates are not verified proceeds.",
            "Daily-close drawdowns use buy-and-hold positions and annual rebalancing, not intraday prices.",
            "Annualized excess is portfolio CAGR minus SPY CAGR, not regression alpha.",
            "If the portfolio has fewer than 16 holdings, the permitted shadow count is rounded down.",
        ],
        "years": [],
    }
    _write_report(output_path, report)
    for year in years:
        progress(f"\nComparing {year}...")
        paired = _PairedProvider(provider)
        original = _arm(paired, year, 13)
        capped = _arm(paired, year, 13, shadow_fraction_limit=0.25)
        pair = {"year": year, "original": original, "capped": capped, "status": "failed"}
        if original["status"] == capped["status"] == "complete":
            try:
                _check_pair(original, capped)
                if original["trace"]["factor_eligible_tickers"] != capped["trace"]["factor_eligible_tickers"]:
                    raise MarketDataError("Paired factor-eligible universes differ.")
                _verify_previous(original, previous[year])
                shadow_count = sum(row["In_Shadow_List"] for row in capped["holdings"])
                if 4 * shadow_count > len(capped["holdings"]):
                    raise MarketDataError("The shadow limit was exceeded.")
            except MarketDataError as exc:
                pair["error"] = str(exc)
            else:
                pair["status"] = "complete"
                pair["shadow_count"] = shadow_count
                pair["return_difference"] = capped["summary"]["Portfolio_Return"] - original["summary"]["Portfolio_Return"]
                progress(
                    f"{year}: original {original['summary']['Portfolio_Return']:.2%} "
                    f"(alpha {original['summary']['Alpha']:+.2%}, drawdown {original['max_drawdown']:.2%}); "
                    f"capped {capped['summary']['Portfolio_Return']:.2%} "
                    f"(alpha {capped['summary']['Alpha']:+.2%}, drawdown {capped['max_drawdown']:.2%}); "
                    f"difference {pair['return_difference']:+.2%}; "
                    f"shadow holdings {shadow_count}/{len(capped['holdings'])}."
                )
        if pair["status"] == "failed":
            errors = [pair.get("error", "")] + [
                f"{name}: {arm.get('error', arm['trace']['backtest_status'])}"
                for name, arm in (("original", original), ("capped", capped))
                if arm["status"] != "complete"
            ]
            progress(f"{year}: incomplete; " + "; ".join(filter(None, errors)))
        report["years"].append(pair)
        _write_report(output_path, report)
    complete = all(pair["status"] == "complete" for pair in report["years"])
    if complete:
        for earlier, later in zip(report["years"], report["years"][1:]):
            for name in ("original", "capped"):
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
            name: _full_period(report["years"], name) for name in ("original", "capped")
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
        parser.error("Choose completed years from 1999 onward.")
    path = OUTPUT_DIR / f"comparison_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}.json"
    try:
        provider = configured_provider(delisting_policy="last_close_cash" if args.estimate_delisted else "strict")
        report = run_comparison(provider, range(args.start_year, args.end_year + 1), args.baseline_report, path)
        print(f"\nPrivate comparison report: {path}")
        failures = [item["year"] for item in report["years"] if item["status"] != "complete"]
        if failures:
            parser.exit(1, "Incomplete years: " + ", ".join(map(str, failures)) + "\n")
        if "full_period_error" in report:
            parser.exit(1, report["full_period_error"] + "\n")
    except (MarketDataError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Comparison failed: {exc}\n")


if __name__ == "__main__":
    main()
