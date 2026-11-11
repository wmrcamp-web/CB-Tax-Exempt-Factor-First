import datetime
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from snapshot_audit import audit_snapshot_coverage, rebalance_dates, write_coverage_report


DATE = datetime.date(2025, 12, 31)


def write_pair(root, symbols=("AAA", "BRK.B"), caps=("10", "0")):
    constituents = root / "constituents" / f"sp500_{DATE}.csv"
    market_caps = root / "market_caps" / f"mcaps_{DATE}.csv"
    constituents.parent.mkdir(parents=True)
    market_caps.parent.mkdir(parents=True)
    pd.DataFrame({
        "Symbol": symbols, "Security": ["Company"] * len(symbols),
        "GICS Sector": ["Technology"] * len(symbols),
        "GICS Sub-Industry": ["Industry"] * len(symbols),
    }).to_csv(constituents, index=False)
    pd.DataFrame({"Symbol": ["AAA", "BRK-B"], "MarketCap": caps}).to_csv(market_caps, index=False)
    return constituents, market_caps


def test_empty_cache_reports_all_required_dates(tmp_path):
    rows = audit_snapshot_coverage(tmp_path)
    assert len(rows) == 12
    assert rows[0]["rebalance_date"] == "2014-12-31"
    assert rows[-1]["rebalance_date"] == "2025-12-31"
    assert all(row["snapshot_status"] == "missing_snapshot" for row in rows)
    assert all(row["would_require_live_fallback"] for row in rows)
    assert not tmp_path.joinpath("_meta.json").exists()
    assert rebalance_dates(2026, 2026) == [DATE]


def test_complete_pair_is_deterministic_but_not_proof_of_historical_source(tmp_path):
    write_pair(tmp_path)
    rows = audit_snapshot_coverage(tmp_path, [DATE])
    assert rows == audit_snapshot_coverage(tmp_path, [DATE])
    row = rows[0]
    assert row["snapshot_complete"]
    assert not row["would_require_live_fallback"]
    assert row["valid_market_cap_count"] == 2
    assert row["symbol_mismatch_count"] == 0
    assert row["provenance_status"] == "unverified"
    assert row["current_provider_source"] == "live_current"
    for suffix in ("json", "csv"):
        report = tmp_path / f"report.{suffix}"
        write_coverage_report(rows, report)
        if suffix == "json":
            assert json.loads(report.read_text()) == rows
        else:
            assert pd.read_csv(report).iloc[0]["snapshot_complete"]


@pytest.mark.parametrize("bad_cap", ["-1", "NaN", "inf", "-inf", "invalid", ""])
def test_invalid_caps_are_not_coverage(tmp_path, bad_cap):
    write_pair(tmp_path, caps=("10", bad_cap))
    row = audit_snapshot_coverage(tmp_path, [DATE])[0]
    assert row["valid_market_cap_count"] == 1
    assert row["invalid_market_cap_count"] == 1
    assert row["invalid_market_cap_tickers"] == ["BRK-B"]
    assert not row["snapshot_complete"]


def test_partial_pair_and_duplicate_normalized_symbols(tmp_path):
    _, caps = write_pair(tmp_path, symbols=("BRK.B", "BRK-B", "MISSING", " "))
    pd.DataFrame({"Symbol": ["BRK.B", "BRK-B"], "MarketCap": [10, 20]}).to_csv(caps, index=False)
    row = audit_snapshot_coverage(tmp_path, [DATE])[0]
    assert row["duplicate_symbol_count"] == 2
    assert row["invalid_symbol_count"] == 1
    assert row["missing_market_cap_tickers"] == ["MISSING"]
    assert row["missing_market_cap_count"] == 1
    assert row["would_require_live_fallback"]


@pytest.mark.parametrize("content", ["", "Symbol\nAAA\n", 'Symbol,MarketCap\n"unterminated',
                                   "Symbol,MarketCap\nx,AAA,10\nx,,20\n"])
def test_unreadable_empty_or_malformed_snapshot(tmp_path, content):
    _, caps = write_pair(tmp_path)
    caps.write_text(content)
    row = audit_snapshot_coverage(tmp_path, [DATE])[0]
    assert not row["snapshot_complete"]
    assert row["fallback_reason"]


def test_manifest_dates_and_invalid_manifest_are_reported(tmp_path):
    write_pair(tmp_path)
    manifest = tmp_path / "_meta.json"
    provenance = {"market_caps": {"as_of_date": "2024-12-31", "methodology": "approximation"}}
    manifest.write_text(json.dumps({str(DATE): provenance}))
    row = audit_snapshot_coverage(tmp_path, [DATE])[0]
    assert not row["snapshot_complete"]
    assert "date mismatch" in row["fallback_reason"]
    assert row["source_provenance"] == provenance
    manifest.write_text("[]")
    assert "invalid manifest" in audit_snapshot_coverage(tmp_path, [DATE])[0]["fallback_reason"]


def test_cli_audits_without_importing_live_apps(tmp_path):
    output = tmp_path / "coverage.json"
    result = subprocess.run([
        sys.executable, str(Path(__file__).with_name("snapshot_audit.py")),
        "--cache-dir", str(tmp_path / "empty"), "--output", str(output),
    ], check=True, capture_output=True, text=True)
    assert "0/12 complete snapshot pairs" in result.stdout
    assert len(json.loads(output.read_text())) == 12
