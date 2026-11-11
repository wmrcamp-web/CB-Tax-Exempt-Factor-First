"""Network-free coverage audit for dated historical input exports."""

import argparse
import datetime
import json
import math
from pathlib import Path

import pandas as pd

from symbol_utils import normalize_symbol


CONSTITUENT_COLUMNS = {"Symbol", "Security", "GICS Sector", "GICS Sub-Industry"}
CAP_COLUMNS = {"Symbol", "MarketCap"}


def rebalance_dates(start_year=2015, end_year=2026):
    """Return prior-year December 31 dates for inclusive backtest return years."""
    return [datetime.date(year - 1, 12, 31) for year in range(start_year, end_year + 1)]


def _read_snapshot(path, required, reasons):
    if not path.is_file():
        reasons.append(f"missing file: {path.name}")
        return pd.DataFrame()
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, ValueError, UnicodeError) as exc:
        reasons.append(f"unreadable file: {path.name} ({type(exc).__name__})")
        return pd.DataFrame()
    missing = sorted(required - set(frame.columns))
    if not frame.index.equals(pd.RangeIndex(len(frame))):
        reasons.append(f"{path.name}: malformed rows")
        frame = frame.reset_index(drop=True)
    if missing:
        reasons.append(f"{path.name}: missing columns {', '.join(missing)}")
    if frame.empty:
        reasons.append(f"{path.name}: empty snapshot")
    return frame


def _symbols(frame):
    raw = frame.get("Symbol", pd.Series(dtype=str))
    valid = raw.map(lambda symbol: bool(symbol) and not any(c.isspace() for c in symbol)).astype(bool)
    # Match protocol_engine.prepare_universe's share-class normalization.
    symbols = raw[valid].map(normalize_symbol)
    return symbols, int((~valid).sum()), int(symbols.duplicated().sum())


def audit_snapshot_coverage(cache_dir, dates=None):
    """Inspect local files only; fallback flags describe a proposed strict policy."""
    root = Path(cache_dir)
    metadata = {}
    metadata_error = None
    metadata_path = root / "_meta.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("manifest must be an object keyed by date")
        except (OSError, ValueError, UnicodeError) as exc:
            metadata = {}
            metadata_error = f"invalid manifest ({type(exc).__name__})"

    rows = []
    for date in rebalance_dates() if dates is None else dates:
        stamp = date.isoformat()
        constituents_path = root / "constituents" / f"sp500_{stamp}.csv"
        caps_path = root / "market_caps" / f"mcaps_{stamp}.csv"
        reasons = []
        constituents = _read_snapshot(constituents_path, CONSTITUENT_COLUMNS, reasons)
        caps = _read_snapshot(caps_path, CAP_COLUMNS, reasons)
        constituent_symbols, invalid_constituents, duplicate_constituents = _symbols(constituents)
        cap_symbols, invalid_cap_symbols, duplicate_caps = _symbols(caps)
        cap_values = pd.to_numeric(caps.get("MarketCap", pd.Series(dtype=str)), errors="coerce")
        valid_values = cap_values.map(lambda value: math.isfinite(value) and value >= 0).astype(bool)
        valid_symbols = set(cap_symbols[valid_values.reindex(cap_symbols.index, fill_value=False)])
        universe = set(constituent_symbols)
        missing_symbols = sorted(universe - set(cap_symbols))
        invalid_symbols = sorted((universe & set(cap_symbols)) - valid_symbols)
        invalid_caps = int((~valid_values).sum())
        if invalid_constituents or invalid_cap_symbols:
            reasons.append("invalid symbols")
        if duplicate_constituents or duplicate_caps:
            reasons.append("duplicate normalized symbols")
        if invalid_caps:
            reasons.append("invalid market caps")
        if missing_symbols or invalid_symbols:
            reasons.append("constituents lack valid market caps")

        provenance = metadata.get(stamp, {})
        provenance_status = "unverified"
        if metadata_error:
            reasons.append(metadata_error)
        elif not isinstance(provenance, dict):
            reasons.append("invalid date manifest")
            provenance = {}
        elif provenance:
            for kind in ("constituents", "market_caps"):
                entry = provenance.get(kind, {})
                if not isinstance(entry, dict):
                    reasons.append(f"invalid {kind} provenance")
                elif entry.get("as_of_date") not in (None, stamp):
                    reasons.append(f"{kind} provenance date mismatch")
            provenance_status = "provided"

        complete = not reasons
        exists = constituents_path.is_file() and caps_path.is_file()
        rows.append({
            "rebalance_date": stamp,
            "portfolio_year": date.year + 1,
            "constituent_snapshot_exists": constituents_path.is_file(),
            "market_cap_snapshot_exists": caps_path.is_file(),
            "constituent_row_count": len(constituents),
            "constituent_unique_symbol_count": len(universe),
            "market_cap_row_count": len(caps),
            "valid_market_cap_count": len(universe & valid_symbols),
            "missing_market_cap_count": len(missing_symbols),
            "invalid_market_cap_count": invalid_caps,
            "invalid_symbol_count": invalid_constituents + invalid_cap_symbols,
            "duplicate_symbol_count": duplicate_constituents + duplicate_caps,
            "symbol_mismatch_count": len(universe ^ set(cap_symbols)),
            "missing_market_cap_tickers": missing_symbols,
            "invalid_market_cap_tickers": invalid_symbols,
            "snapshot_complete": complete,
            "snapshot_status": "complete_snapshot" if complete else (
                "missing_snapshot" if not exists else "invalid_snapshot"
            ),
            "would_require_live_fallback": not complete,
            "fallback_reason": "; ".join(reasons),
            "current_provider_source": "live_current",
            "provenance_status": provenance_status,
            "source_provenance": provenance,
        })
    return rows


def write_coverage_report(rows, output_path):
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        flattened = [
            {key: json.dumps(value, sort_keys=True) if isinstance(value, (list, dict)) else value
             for key, value in row.items()}
            for row in rows
        ]
        pd.DataFrame(flattened).to_csv(path, index=False)
    else:
        path.write_text(json.dumps(rows, indent=2, sort_keys=True, allow_nan=False) + "\n",
                        encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--start-year", type=int, default=2015, help="First return year")
    parser.add_argument("--end-year", type=int, default=2026, help="Last return year")
    parser.add_argument("--output", type=Path, required=True, help="CSV or JSON report path")
    args = parser.parse_args()
    if not 2 <= args.start_year <= args.end_year <= 9999:
        parser.error("return years must satisfy 2 <= start-year <= end-year <= 9999")
    rows = audit_snapshot_coverage(args.cache_dir, rebalance_dates(args.start_year, args.end_year))
    write_coverage_report(rows, args.output)
    print(f"{sum(row['snapshot_complete'] for row in rows)}/{len(rows)} complete snapshot pairs")


if __name__ == "__main__":
    main()
