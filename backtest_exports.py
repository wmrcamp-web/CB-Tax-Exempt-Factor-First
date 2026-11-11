"""Validated exports where annual summary and detail rows come from one yearly run."""

import datetime
import io
from pathlib import Path
from threading import Lock

import numpy as np
import pandas as pd

from backtest_engine import (
    DETAILED_COLUMNS, RECONCILIATION_TOLERANCE, ReconciliationError, archive_path, archive_rows,
    atomic_write_csv, compute_backtest_year, integer_years, reconcile_year,
)


DATA_DIR = Path(__file__).resolve().parent
SOURCE_COLUMN = "Detail_Source"
GENERATED_SOURCE = "Generated with its annual summary row in one run; reconciled"
SUMMARY_COLUMNS = ["Year", "Portfolio_Return", "Benchmark_Return", "Alpha"]
_DETAILS_LOCK = Lock()
_SUMMARY_LOCK = Lock()


def normalize_years(values):
    years = pd.to_numeric(values, errors="coerce")
    return years.where(
        np.isfinite(years) & (years == years.round()) &
        (years >= 1) & (years < datetime.date.today().year)
    ).astype("Int64")


def read_summary(path):
    source = Path(path).read_bytes()
    frame = pd.read_csv(io.BytesIO(source), float_precision="round_trip")
    required = {"Year", "Portfolio_Return", "Benchmark_Return"}
    if not required.issubset(frame.columns):
        raise ValueError("Annual summary requires Year, Portfolio_Return and Benchmark_Return columns.")
    frame["Year"] = normalize_years(frame["Year"])
    for column in ("Portfolio_Return", "Benchmark_Return"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    valid = frame["Year"].notna() & np.isfinite(frame["Portfolio_Return"]) & np.isfinite(frame["Benchmark_Return"])
    frame = frame.loc[valid].drop_duplicates("Year").sort_values("Year").copy()
    frame["Year"] = frame["Year"].astype(int)
    return source, frame


def validate_details(frame):
    """Reject invalid year groups, not individual holdings that would change weights."""
    if not set(DETAILED_COLUMNS).issubset(frame.columns):
        return pd.DataFrame(columns=DETAILED_COLUMNS + [SOURCE_COLUMN])
    frame = frame.copy()
    frame["Year"] = normalize_years(frame["Year"])
    frame = frame.dropna(subset=["Year"])
    frame["Year"] = frame["Year"].astype(int)
    frame["Ticker"] = frame["Ticker"].astype("string").str.strip()
    numeric = [column for column in DETAILED_COLUMNS if column not in ("Year", "Ticker")]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    valid_years = []
    for year, group in frame.groupby("Year"):
        if (group["Ticker"].notna().all() and group["Ticker"].ne("").all()
                and not group["Ticker"].duplicated().any()
                and np.isfinite(group["Weight"]).all()
                and (group["Weight"] > 0).all()
                and np.allclose(group["Weight"], 1 / len(group))
                and np.allclose(group["Weight_Percent"], group["Weight"] * 100)
                and np.isfinite(group[["Portfolio_Return", "Benchmark_Return", "Alpha"]]).all().all()
                and np.isfinite(group["Annual_Return"]).any()
                and not np.isinf(group[numeric]).any().any()):
            valid_years.append(year)
    frame = frame[frame["Year"].isin(valid_years)].copy()
    if SOURCE_COLUMN not in frame:
        frame[SOURCE_COLUMN] = "Existing file; historical provenance unverified"
    frame[SOURCE_COLUMN] = frame[SOURCE_COLUMN].fillna("Existing file; historical provenance unverified")
    return frame


def read_details(path):
    try:
        frame = pd.read_csv(path, float_precision="round_trip")
    except FileNotFoundError:
        return validate_details(pd.DataFrame()), None
    except (OSError, ValueError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        return validate_details(pd.DataFrame()), f"Could not read detailed holdings: {exc}"
    valid = validate_details(frame)
    problem = None
    if not frame.empty and (valid.empty or len(valid) != len(frame)):
        problem = "Invalid or incomplete detailed holdings were excluded; missing years can be regenerated."
    return valid, problem


def merge_details(existing, generated):
    return pd.concat([
        existing[~existing["Year"].isin(generated["Year"])], generated
    ], ignore_index=True).sort_values(["Year", "Ticker"]).reset_index(drop=True)


def merge_summary(summary, rebuilt):
    """Replace summary years with rows rebuilt together with their details."""
    if rebuilt is None or rebuilt.empty:
        return summary
    return pd.concat([
        summary[~summary["Year"].isin(rebuilt["Year"])], rebuilt
    ], ignore_index=True).sort_values("Year").reset_index(drop=True)


def reconcile_details(summary, details, tolerance=RECONCILIATION_TOLERANCE):
    """Return detail rows publishable for summary years plus a reason for each other year."""
    problems = {}
    reconciled = set()
    for row in summary.to_dict("records"):
        year = int(row["Year"])
        group = details[details["Year"] == year]
        if group.empty:
            continue
        try:
            reconcile_year(row, group.to_dict("records"), tolerance)
            reconciled.add(year)
        except ReconciliationError as exc:
            problems[year] = str(exc)
    return details[details["Year"].isin(reconciled)].copy(), problems


def rebuild_years(years, provider=None, progress=None):
    """Run the canonical yearly backtest once per year for matching summary and detail rows."""
    years = sorted(set(normalize_years(pd.Series(list(years))).dropna().astype(int)))
    summaries, details, failures = [], [], {}
    for index, year in enumerate(years):
        if progress is not None:
            progress(index, len(years), year)
        traces = []
        try:
            result = compute_backtest_year(year, provider=provider, traces=traces)
            if result is None:
                notes = [note for trace in traces for note in trace.selection_notes]
                statuses = [trace.backtest_status for trace in traces if trace.backtest_status]
                raise ValueError("; ".join(notes + statuses) or "No valid holdings/prices returned.")
            summaries.append(result.summary)
            details.extend(result.details)
        except Exception as exc:
            failures[year] = str(exc)
    if progress is not None and years:
        progress(len(years), len(years), years[-1])
    summary = pd.DataFrame(summaries, columns=SUMMARY_COLUMNS)
    generated = validate_details(pd.DataFrame(details, columns=DETAILED_COLUMNS))
    generated[SOURCE_COLUMN] = GENERATED_SOURCE
    return summary, generated, failures


def persist_summary(rebuilt, path):
    """Replace rebuilt years in the summary file, archiving the replaced rows."""
    with _SUMMARY_LOCK:
        try:
            current = pd.read_csv(path, float_precision="round_trip")
        except FileNotFoundError:
            current = pd.DataFrame(columns=SUMMARY_COLUMNS)
        years = normalize_years(current["Year"]) if "Year" in current else pd.Series(dtype="Int64")
        replaced = years.isin(rebuilt["Year"]).fillna(False).to_numpy(dtype=bool)
        archive_rows(current[replaced], archive_path(path))
        merged = pd.concat([current[~replaced], rebuilt], ignore_index=True)
        merged = integer_years(merged.sort_values(
            "Year", key=lambda column: pd.to_numeric(column, errors="coerce")).reset_index(drop=True))
        atomic_write_csv(merged, path)
        return merged


def persist_details(frame, path):
    """Merge concurrent Streamlit sessions, archive replaced years and atomically replace the export."""
    with _DETAILS_LOCK:
        current, _ = read_details(path)
        archive_rows(current[current["Year"].isin(frame["Year"])], archive_path(path))
        merged = merge_details(current, frame)
        atomic_write_csv(merged, path)
        return merged
