"""Run isolated Sharadar backtests or check direct-API table access."""

import argparse
from datetime import date
import os
from pathlib import Path

from data_errors import MarketDataError
from sharadar_provider import SharadarClient, SharadarProvider


DATA_DIR = Path(__file__).resolve().parent / ".data" / "sharadar"


def configured_provider(settings=None, *, delisting_policy="strict"):
    settings = {} if settings is None else settings
    key = os.environ.get("SHARADAR_API_KEY") or settings.get("SHARADAR_API_KEY")
    gics_dir = (
        os.environ.get("SHARADAR_GICS_DIR") or settings.get("SHARADAR_GICS_DIR")
        or DATA_DIR / "gics"
    )
    sector_source = (
        os.environ.get("SHARADAR_SECTOR_SOURCE") or settings.get("SHARADAR_SECTOR_SOURCE")
        or "sharadar"
    )
    return SharadarProvider(
        key, gics_dir, sector_source=sector_source, delisting_policy=delisting_policy,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-access", action="store_true")
    parser.add_argument("--start-year", type=int, default=2016)
    parser.add_argument("--end-year", type=int, default=date.today().year - 1)
    parser.add_argument(
        "--estimate-delisted", action="store_true",
        help="Estimate confirmed delisted holdings using last adjusted close, then 0%% cash; "
             "actual payouts are unverified and bankruptcy returns can be overstated.",
    )
    args = parser.parse_args()
    if not 1999 <= args.start_year <= args.end_year < date.today().year:
        parser.error("Choose completed portfolio years from 1999 onward.")
    try:
        if args.check_access:
            client = SharadarClient(os.environ.get("SHARADAR_API_KEY"))
            for table in ("sp500", "daily", "stocks", "funds", "tickers"):
                fields = {
                    "sp500": ["ticker", "date", "action", "name"],
                    "daily": ["ticker", "date", "marketcap"],
                    "stocks": ["ticker", "date", "closeadj"],
                    "funds": ["ticker", "date", "closeadj"],
                    "tickers": ["ticker", "name", "sector", "industry", "isdelisted",
                                "lastpricedate", "permaticker"],
                }[table]
                filters = (
                    {"table": "fundamentals"} if table == "tickers" else
                    {"from": "2025-09-01", "to": "2025-12-31"}
                )
                frame = client.table(
                    table, fields, ticker="SPY" if table == "funds" else "AAPL",
                    **filters,
                )
                if frame.empty:
                    raise MarketDataError(f"Sharadar {table} returned no sample observations.")
                print(f"{table}: access confirmed")
        else:
            from backtest_engine import update_backtest_history

            provider = configured_provider(
                delisting_policy="last_close_cash" if args.estimate_delisted else "strict",
            )
            if provider.sector_source == "sharadar":
                print("LIMITATION: using current Sharadar sector labels, not dated GICS.")
            if args.estimate_delisted:
                print("LIMITATION: confirmed delisted exits may use last-close cash estimates; "
                      "actual merger/bankruptcy proceeds are unverified.")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            update_backtest_history(
                csv_path=DATA_DIR / "backtest_results.csv",
                detailed_csv_path=DATA_DIR / "backtest_detailed_results.csv",
                trace_path=DATA_DIR / "selection_traces.json",
                start_year=args.start_year, end_year=args.end_year,
                provider=provider,
            )
            from backtest_exports import read_summary

            summary_path = DATA_DIR / "backtest_results.csv"
            if not summary_path.exists():
                raise MarketDataError(
                    "No Sharadar years were published. See the errors above and "
                    "private selection_traces.json."
                )
            _, summary = read_summary(summary_path)
            missing = set(range(args.start_year, args.end_year + 1)) - set(summary["Year"])
            if missing:
                raise MarketDataError(
                    "No published results for years: " + ", ".join(map(str, sorted(missing))) +
                    ". See the errors above and private selection_traces.json."
                )
    except (MarketDataError, FileNotFoundError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
