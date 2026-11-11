import pandas as pd
from dataclasses import dataclass
import datetime
import math
import os
from pathlib import Path
import tempfile

from portfolio_pipeline import BENCHMARK_GATE, run_pipeline
from data_errors import MarketDataError
from data_provider import DEFAULT_PROVIDER
from selection_trace import SelectionTrace, write_selection_traces

DETAILED_COLUMNS = [
    "Year", "Ticker", "Weight", "Weight_Percent", "Start_Price", "End_Price",
    "Annual_Return", "Portfolio_Return", "Benchmark_Return", "Alpha",
]
# Weighted detail returns must match the summary portfolio return within this absolute tolerance.
RECONCILIATION_TOLERANCE = 1e-9


class ReconciliationError(ValueError):
    """Detail rows do not exactly decompose the annual summary row."""


@dataclass
class YearlyBacktest:
    """One year's annual summary row and the ticker rows it was calculated from."""
    summary: dict
    details: list


def reconcile_year(summary, details, tolerance=RECONCILIATION_TOLERANCE):
    """Raise ReconciliationError unless details are the row-level decomposition of summary."""
    def number(value, label):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ReconciliationError(f"{label} is not numeric") from None
        if not math.isfinite(value):
            raise ReconciliationError(f"{label} is missing or not finite")
        return value

    def close(left, right):
        return math.isclose(left, right, rel_tol=0.0, abs_tol=tolerance)

    year = number(summary.get("Year"), "Summary year")
    label = f"{int(year)}" if year == round(year) else f"{year}"
    if not details:
        raise ReconciliationError(f"{label}: no detailed holdings")
    portfolio = number(summary.get("Portfolio_Return"), f"{label} summary portfolio return")
    benchmark = number(summary.get("Benchmark_Return"), f"{label} summary benchmark return")
    alpha = portfolio - benchmark
    summary_alpha = pd.to_numeric(summary.get("Alpha"), errors="coerce")
    if pd.notna(summary_alpha) and not close(float(summary_alpha), alpha):
        raise ReconciliationError(f"{label}: summary alpha is not portfolio minus benchmark return")
    tickers = [row.get("Ticker") for row in details]
    if any(not isinstance(ticker, str) or not ticker.strip() for ticker in tickers) \
            or len(set(tickers)) != len(tickers):
        raise ReconciliationError(f"{label}: tickers must be unique and non-blank")
    weighted = total_weight = 0.0
    for row in details:
        ticker = row["Ticker"]
        if number(row.get("Year"), f"{label} {ticker} year") != year:
            raise ReconciliationError(f"{label}: {ticker} row belongs to a different year")
        weight = number(row.get("Weight"), f"{label} {ticker} weight")
        if not close(weight, 1.0 / len(details)):
            raise ReconciliationError(f"{label}: {ticker} weight is not an equal 1/N weight")
        start = number(row.get("Start_Price"), f"{label} {ticker} start price")
        end = number(row.get("End_Price"), f"{label} {ticker} end price")
        ticker_return = number(row.get("Annual_Return"), f"{label} {ticker} annual return")
        if start <= 0 or not close(end / start - 1.0, ticker_return):
            raise ReconciliationError(f"{label}: {ticker} return does not match its start/end prices")
        for column, expected in (("Portfolio_Return", portfolio), ("Benchmark_Return", benchmark),
                                 ("Alpha", alpha)):
            if not close(number(row.get(column), f"{label} {ticker} {column}"), expected):
                raise ReconciliationError(f"{label}: {ticker} {column} differs from the annual summary")
        weighted += weight * ticker_return
        total_weight += weight
    if not close(total_weight, 1.0):
        raise ReconciliationError(f"{label}: weights do not sum to 1")
    if not close(weighted, portfolio):
        raise ReconciliationError(
            f"{label}: weighted holdings return {weighted:.6%} does not match summary "
            f"portfolio return {portfolio:.6%}")


def compute_backtest_year(target_year, provider=None, traces=None):
    """
    Simulates running the protocol on Dec 31 of (target_year - 1) and tracking 
    the portfolio's performance through Dec 31 of target_year.

    Returns a YearlyBacktest whose summary and detail rows come from this single
    calculation and have passed reconcile_year, or None if the year cannot be published.
    """
    print(f"\n--- Computing Backtest for {target_year} ---")
    provider = DEFAULT_PROVIDER if provider is None else provider
    
    # 1. Run Phase 1-5 selection as of Dec 31 of the previous year
    # Note: If Dec 31 is a weekend, yfinance handles fetching the closest trading day implicitly in our logic
    start_eval_date = datetime.date(target_year - 1, 12, 31)
    end_eval_date = datetime.date(target_year, 12, 31)
    
    print(f"Running protocol selection as of {start_eval_date}...")
    
    # Derive the default gate from the same SPY window as the selection run.
    trace = SelectionTrace() if traces is not None else None
    if trace is not None:
        trace.begin(start_eval_date, None, provider)
        traces.append(trace)
    # Run the pipeline
    # run_pipeline returns: sp500_ret, metrics_df, loadings_df, portfolio_df, survivors_df, factor_headers
    try:
        trace_kwargs = {"trace": trace} if trace is not None else {}
        results = run_pipeline(start_eval_date, BENCHMARK_GATE, provider=provider, **trace_kwargs)
        portfolio_df = results[3]
    except Exception as e:
        if trace is not None:
            if isinstance(e, MarketDataError):
                # The pipeline normally records market-data failures with a specific status.
                if trace.status != e.trace_status:
                    trace.fail(e)
            else:
                trace.status = "selection_failed"
                trace.selection_notes.append(str(e))
            trace.backtest_status = trace.status
        print(f"Error running pipeline for {target_year}: {e}")
        return None
        
    if portfolio_df.empty:
        if trace is not None:
            trace.backtest_status = "no_portfolio"
        print(f"No portfolio generated for {target_year}.")
        return None
        
    final_tickers = portfolio_df.index.tolist()
    print(f"Selected {len(final_tickers)} tickers for {target_year} portfolio.")
    
    # 2. Download price data for those tickers from start_eval_date to end_eval_date to compute actual forward return
    # DECISION (sampling frequency): forward returns are measured with weekly ("1wk") bars so the
    # backtest reflects the same weekly sampling as the selection pipeline in app.py.
    # The forward return is an endpoint calculation ((last / first) - 1), so weekly vs daily
    # sampling changes results only marginally, but weekly keeps both stages consistent and is
    # more robust to missing daily bars (holidays/halts) over the 1-year holding window.
    print(f"Downloading forward performance data for {target_year}...")
    all_tickers = final_tickers + ["SPY"]
    
    # Fetch data
    try:
        data = provider.get_weekly_closes(all_tickers, start_eval_date, end_eval_date)
        # bfill() then ffill() ensures we have a valid start and end price even if a stock halted or delisted mid-year
        data = data.bfill().ffill()
    except Exception as e:
        if trace is not None:
            trace.backtest_status = "forward_data_failed"
            trace.selection_notes.append(str(e))
        print(f"Error downloading forward data for {target_year}: {e}")
        return None
        
    if data.empty:
         if trace is not None:
             trace.backtest_status = "forward_data_empty"
         print(f"Failed to download pricing data for forward returns in {target_year}.")
         return None
         
    # 3. Calculate actual forward returns once; summary and detail rows both derive from them.
    # (Last valid price / First valid price) - 1
    start_prices, end_prices = data.iloc[0], data.iloc[-1]
    returns = (end_prices / start_prices) - 1.0

    benchmark_return = float(returns.get("SPY", 0.0))
    if trace is not None:
        trace.forward_benchmark_return = benchmark_return

    # Equal weight portfolio return = sum of each holding's return x its weight. A missing
    # holding return makes the year fail reconciliation rather than silently leaving the average.
    weight = 1.0 / len(final_tickers)
    portfolio_return = float(sum(weight * returns.get(ticker, float("nan")) for ticker in final_tickers))
    alpha = portfolio_return - benchmark_return
    missing = [ticker for ticker in sorted(final_tickers)
               if not math.isfinite(float(returns.get(ticker, float("nan"))))]
    if missing:
        if trace is not None:
            trace.backtest_status = "missing_prices"
            trace.selection_notes.append(
                f"{target_year}: missing holding-year prices for {', '.join(missing)}; "
                "equal-weight portfolio return cannot be reconciled")
        print(f"[{target_year}] Not published: missing holding-year prices for {', '.join(missing)}.")
        return None

    summary = {
        "Year": target_year,
        "Portfolio_Return": portfolio_return,
        "Benchmark_Return": benchmark_return,
        "Alpha": alpha,
    }
    details = [{
        "Year": target_year,
        "Ticker": ticker,
        "Weight": weight,
        "Weight_Percent": weight * 100,
        "Start_Price": start_prices.get(ticker),
        "End_Price": end_prices.get(ticker),
        "Annual_Return": returns.get(ticker),
        "Portfolio_Return": portfolio_return,
        "Benchmark_Return": benchmark_return,
        "Alpha": alpha,
    } for ticker in sorted(final_tickers)]

    try:
        reconcile_year(summary, details)
    except ReconciliationError as e:
        if trace is not None:
            trace.backtest_status = "reconciliation_failed"
            trace.selection_notes.append(str(e))
        print(f"[{target_year}] Not published: {e}")
        return None
    if trace is not None:
        trace.backtest_status = "completed"

    print(f"[{target_year}] Portfolio: {portfolio_return:.2%} | Benchmark: {benchmark_return:.2%} | Alpha: {alpha:.2%}")
    return YearlyBacktest(summary, details)


def run_backtest_for_year(target_year, provider=None, traces=None, detailed_rows=None):
    """Return the yearly summary; detailed_rows receives the same run's reconciled holdings."""
    result = compute_backtest_year(target_year, provider=provider, traces=traces)
    if result is None:
        return None
    if detailed_rows is not None:
        detailed_rows.extend(result.details)
    return result.summary


def reconciled_years(summary, details, tolerance=RECONCILIATION_TOLERANCE):
    """Years whose detail rows exactly decompose the matching summary row."""
    years = set()
    if summary.empty or details.empty or not set(DETAILED_COLUMNS).issubset(details.columns):
        return years
    detail_years = pd.to_numeric(details["Year"], errors="coerce")
    for row in summary.to_dict("records"):
        year = pd.to_numeric(row.get("Year"), errors="coerce")
        if pd.isna(year) or year != round(year):
            continue
        try:
            reconcile_year(row, details[detail_years == year].to_dict("records"), tolerance)
        except ReconciliationError:
            continue
        years.add(int(year))
    return years


def atomic_write_csv(frame, path):
    """Write a CSV through a same-directory temporary file and atomic replace."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=Path(path).parent,
                                         suffix=".csv", delete=False) as handle:
            temporary = handle.name
            frame.to_csv(handle, index=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def integer_years(frame):
    """Write whole-number years as integers when every year is numeric (e.g. 2024, not 2024.0)."""
    years = pd.to_numeric(frame["Year"], errors="coerce")
    if years.notna().all() and (years == years.round()).all():
        frame = frame.assign(Year=years.astype("int64"))
    return frame


def archive_path(path):
    path = Path(path)
    return path.with_name(f"{path.stem}_archive{path.suffix or '.csv'}")


def archive_rows(rows, path):
    """Append replaced rows to an archive CSV instead of silently discarding them."""
    if rows.empty:
        return
    rows = rows.copy()
    rows["Archived_At"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if os.path.exists(path):
        rows = pd.concat([pd.read_csv(path, float_precision="round_trip"), rows], ignore_index=True)
    atomic_write_csv(rows, path)


def update_backtest_history(csv_path=Path(__file__).resolve().parent / "backtest_results.csv", start_year=2016, provider=None,
                            trace_path=None, detailed_csv_path=None, end_year=None):
    """Write summary and detail rows for each unreconciled completed year from one calculation.

    Summary or detail rows that are replaced are appended to ``*_archive.csv`` companions.
    """
    current_year = datetime.date.today().year
    end_year = min(current_year - 1, end_year if end_year is not None else current_year - 1)
    if detailed_csv_path is None:
        detailed_csv_path = os.path.join(os.path.dirname(csv_path), "backtest_detailed_results.csv")
    if os.path.abspath(csv_path) == os.path.abspath(detailed_csv_path):
        raise ValueError("Summary and detailed CSV paths must be different.")

    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path, float_precision="round_trip")
        print(f"Found existing backtest data with {len(df)} summary rows.")
    else:
        df = pd.DataFrame(columns=["Year", "Portfolio_Return", "Benchmark_Return", "Alpha", "Holdings_Returns"])
        print("No existing backtest data found. Starting fresh.")

    if os.path.exists(detailed_csv_path):
        detailed_df = pd.read_csv(detailed_csv_path, float_precision="round_trip")
    else:
        detailed_df = pd.DataFrame(columns=DETAILED_COLUMNS)
    reconciled = reconciled_years(df, detailed_df)
    results = {}
    traces = [] if trace_path is not None else None
    # Recalculate any completed year whose summary and details are not one reconciled run.
    for year in range(start_year, end_year + 1):
        if year in reconciled:
            continue
        trace_kwargs = {"traces": traces} if traces is not None else {}
        try:
            result = compute_backtest_year(year, provider=provider, **trace_kwargs)
        finally:
            if traces:
                try:
                    write_selection_traces(traces, trace_path)
                except Exception as e:
                    print(f"Error writing selection traces to {trace_path}: {e}")
        if result is not None:
            results[year] = result

    if results:
        years = set(results)
        summary_years = pd.to_numeric(df["Year"], errors="coerce")
        detail_years = pd.to_numeric(detailed_df["Year"], errors="coerce")
        archive_rows(df[summary_years.isin(years)], archive_path(csv_path))
        archive_rows(detailed_df[detail_years.isin(years)], archive_path(detailed_csv_path))
        new_summary = pd.DataFrame([results[year].summary for year in sorted(years)])
        df = pd.concat([df[~summary_years.isin(years)], new_summary], ignore_index=True)
        df = integer_years(df.sort_values(
            by="Year", key=lambda col: pd.to_numeric(col, errors="coerce")).reset_index(drop=True))
        new_details = pd.DataFrame(
            [row for year in sorted(years) for row in results[year].details], columns=DETAILED_COLUMNS)
        detailed_df = pd.concat([detailed_df[~detail_years.isin(years)], new_details], ignore_index=True)
        detailed_df = detailed_df.sort_values(["Year", "Ticker"]).reset_index(drop=True)
        atomic_write_csv(df, csv_path)
        atomic_write_csv(detailed_df, detailed_csv_path)
        print(f"\nWrote reconciled summary and details for {len(years)} years to {csv_path} "
              f"and {detailed_csv_path}.")
    else:
        print("\nBacktest results are already reconciled and up to date or no valid years were generated!")
        if not os.path.exists(detailed_csv_path):
            pd.DataFrame(columns=DETAILED_COLUMNS).to_csv(detailed_csv_path, index=False)

if __name__ == "__main__":
    update_backtest_history()
