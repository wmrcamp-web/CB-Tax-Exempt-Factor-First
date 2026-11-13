"""Verify the comparison changes only the membership boundary and publishes paired results."""

from datetime import date
import json

import pandas as pd
import pytest

import compare_membership as comparison
from backtest_engine import YearlyBacktest
from data_errors import MarketDataError
from sharadar_provider import SharadarProvider


class FixtureClient:
    def __init__(self):
        self.members = [f"T{i:03}" for i in range(500)]

    def table(self, table_name, fields, **filters):
        if table_name == "sp500":
            assert filters["action"] == "current"
            return pd.DataFrame([
                {"date": "2025-12-31", "action": "current", "ticker": t, "name": t}
                for t in self.members
            ])[fields]
        assert table_name == "tickers" and filters["table"] == "stocks"
        rows = [{
            "ticker": t, "name": t, "sector": "Industrials", "industry": "Manufacturing",
            "isdelisted": "N", "lastpricedate": "2025-12-31", "permaticker": t,
            "firstpricedate": "2020-01-01" if t == "T000" else "2010-01-01",
        } for t in filters["ticker"].split(",")]
        return pd.DataFrame(rows)[fields]


def make_provider(tmp_path):
    return SharadarProvider(
        "fixture-only", tmp_path, client=FixtureClient(),
        sector_source="sharadar", delisting_policy="last_close_cash",
    )


def test_current_roster_is_frozen_and_omits_companies_not_yet_priced(tmp_path):
    provider = make_provider(tmp_path)
    biased = comparison.CurrentMembershipProvider(provider)
    early = biased.get_constituents(date(2019, 12, 31))
    assert len(early) == 499
    assert "T000" not in set(early.Symbol)
    assert "T000" in biased.members
    assert biased.snapshot_date == "2025-12-31"
    assert any("T000" in note for note in biased.notes)
    provider.client.members = []
    later = biased.get_constituents(date(2020, 12, 31))
    assert len(later) == 500
    assert "T000" in set(later.Symbol)
    assert biased.delisting_policy == provider.delisting_policy
    assert biased.get_market_caps == provider.get_market_caps


def test_current_snapshot_must_be_complete(tmp_path):
    provider = make_provider(tmp_path)
    provider.client.members = provider.client.members[:30]
    with pytest.raises(MarketDataError, match="snapshot"):
        comparison.CurrentMembershipProvider(provider).get_constituents(date(2019, 12, 31))


def test_missing_first_price_date_fails_instead_of_dropping_security(tmp_path):
    provider = make_provider(tmp_path)
    original = provider.client.table

    def missing_date(table_name, fields, **filters):
        frame = original(table_name, fields, **filters)
        if "firstpricedate" in fields and "T001" in set(frame.ticker):
            frame.loc[frame.ticker == "T001", "firstpricedate"] = None
        return frame

    provider.client.table = missing_date
    with pytest.raises(MarketDataError, match="Missing first-price date"):
        comparison.CurrentMembershipProvider(provider).get_constituents(date(2019, 12, 31))


def test_paired_results_save_distinct_traces_and_do_not_mix_failed_years(tmp_path, monkeypatch):
    provider = make_provider(tmp_path)
    outfile = tmp_path / "comparison.json"
    calls = []

    def fake_compute(year, provider, traces):
        from selection_trace import SelectionTrace

        calls.append((year, type(provider).__name__))
        biased = isinstance(provider, comparison.CurrentMembershipProvider)
        trace = SelectionTrace(
            portfolio_year=year, gate_mode="benchmark",
            benchmark_window_start="2018-12-31", benchmark_window_end="2019-12-30",
            gate_threshold=0.1, backtest_status="completed",
        )
        traces.append(trace)
        if year == 2021 and biased:
            trace.backtest_status = "forward_data_failed"
            trace.selection_notes.append("Missing terminal value")
            return None
        value = 0.2 if biased else 0.1
        return YearlyBacktest(
            {"Year": year, "Portfolio_Return": value, "Benchmark_Return": 0.1, "Alpha": value - 0.1},
            [{"Year": year, "Ticker": "T001", "Annual_Return": value}],
        )

    monkeypatch.setattr(comparison, "compute_backtest_year", fake_compute)
    report = comparison.run_comparison(provider, [2020, 2021], outfile, progress=lambda _: None)
    assert calls == [(2020, "SharadarProvider"), (2020, "CurrentMembershipProvider"),
                     (2021, "SharadarProvider"), (2021, "CurrentMembershipProvider")]
    assert report["years"][0]["return_difference"] == pytest.approx(0.1)
    assert report["years"][1]["status"] == "failed"
    assert "return_difference" not in report["years"][1]
    saved = json.loads(outfile.read_text())
    assert len(saved["current_members"]) == 500
    assert saved["current_member_first_price_dates"]["T000"] == "2020-01-01"
    assert saved["current_member_security_ids"]["T000"] == "T000"
    assert saved["delisting_policy"] == "last_close_cash"
    assert saved["years"][1]["current_membership"]["trace"]["selection_notes"] == ["Missing terminal value"]
    assert "full_period" not in saved
    assert all(len(value) == 64 for value in saved["source_sha256"].values())


def test_pair_with_inconsistent_benchmark_is_not_published():
    a = {"trace": {"gate_mode": "benchmark", "gate_threshold": 0.1,
                   "benchmark_window_start": "2018-12-31", "benchmark_window_end": "2019-12-30"},
         "summary": {"Benchmark_Return": 0.12}}
    b = {"trace": dict(a["trace"]), "summary": {"Benchmark_Return": 0.13}}
    with pytest.raises(MarketDataError, match="comparison withheld"):
        comparison._compatible(a, b)


def test_strict_sector_source_rejected(tmp_path):
    provider = make_provider(tmp_path)
    provider.sector_source = "dated_gics"
    with pytest.raises(MarketDataError, match="Sharadar sector"):
        comparison.CurrentMembershipProvider(provider)
