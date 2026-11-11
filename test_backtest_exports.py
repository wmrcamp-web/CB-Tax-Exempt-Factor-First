import datetime
from pathlib import Path

import pandas as pd
import pytest

from test_weekly_sampling import app_modules


def install_backtest_inputs(bt, monkeypatch, missing=False):
    portfolio = pd.DataFrame({"Weight (%)": [33.33] * 3}, index=["CCC", "AAA", "BBB"])
    monkeypatch.setattr(bt, "run_pipeline", lambda *args, **kwargs: (
        0.1, pd.DataFrame(), pd.DataFrame(), portfolio, pd.DataFrame(), []))

    class Provider:
        calls = []

        def get_benchmark_return(self, date):
            return 0.1

        def get_weekly_closes(self, tickers, start, end):
            self.calls.append(end.year)
            return pd.DataFrame({
                "AAA": [100.0, 120.0],
                "BBB": [100.0, 90.0],
                "CCC": [float("nan"), float("nan")] if missing else [50.0, 55.0],
                "SPY": [100.0, 108.0],
            })

    return Provider()


def test_summary_and_detailed_rows_come_from_one_reconciled_calculation(app_modules, monkeypatch):
    bt = app_modules["backtest_engine"]
    provider = install_backtest_inputs(bt, monkeypatch)
    result = bt.compute_backtest_year(2025, provider=provider)
    rows = []
    summary = bt.run_backtest_for_year(2025, provider=provider, detailed_rows=rows)
    assert summary == result.summary
    assert rows == result.details
    assert set(summary) == {"Year", "Portfolio_Return", "Benchmark_Return", "Alpha"}
    details = pd.DataFrame(rows)
    assert details.columns.tolist() == bt.DETAILED_COLUMNS
    assert details["Ticker"].tolist() == ["AAA", "BBB", "CCC"]
    assert details["Year"].tolist() == [2025] * 3
    assert details["Weight"].tolist() == pytest.approx([1 / 3] * 3)
    assert details["Weight"].sum() == pytest.approx(1.0)
    assert details["Weight_Percent"].sum() == pytest.approx(100.0)
    assert details["Annual_Return"].tolist() == pytest.approx([0.2, -0.1, 0.1])
    assert details["Annual_Return"].tolist() == pytest.approx(
        (details["End_Price"] / details["Start_Price"] - 1).tolist())
    assert (details["Weight"] * details["Annual_Return"]).sum() == pytest.approx(
        summary["Portfolio_Return"], abs=bt.RECONCILIATION_TOLERANCE)
    assert summary["Portfolio_Return"] == pytest.approx(0.2 / 3)
    assert summary["Benchmark_Return"] == pytest.approx(0.08)
    for column in ("Portfolio_Return", "Benchmark_Return", "Alpha"):
        assert details[column].tolist() == [summary[column]] * 3
    assert rows[0]["Start_Price"] == 100.0
    assert rows[0]["End_Price"] == 120.0
    bt.reconcile_year(summary, rows)
    assert bt.reconciled_years(pd.DataFrame([summary]), details) == {2025}


def test_missing_holding_prices_fail_reconciliation_instead_of_leaving_the_average(
        app_modules, monkeypatch):
    bt = app_modules["backtest_engine"]
    provider = install_backtest_inputs(bt, monkeypatch, missing=True)
    rows, traces = [], []
    assert bt.run_backtest_for_year(2025, provider=provider, traces=traces, detailed_rows=rows) is None
    assert rows == []
    assert traces[0].backtest_status == "missing_prices"
    assert any("CCC" in note for note in traces[0].selection_notes)


@pytest.mark.parametrize("change, message", [
    ("ticker_return", "start/end prices"),
    ("prices_and_return", "weighted holdings return"),
    ("summary_return", "differs from the annual summary"),
    ("benchmark", "Benchmark_Return differs"),
    ("weight", "equal 1/N weight"),
    ("dropped_holding", "equal 1/N weight"),
    ("no_rows", "no detailed holdings"),
])
def test_drifted_details_fail_reconciliation(app_modules, monkeypatch, change, message):
    bt = app_modules["backtest_engine"]
    result = bt.compute_backtest_year(2025, provider=install_backtest_inputs(bt, monkeypatch))
    summary, rows = dict(result.summary), [dict(row) for row in result.details]
    if change == "ticker_return":
        rows[0]["Annual_Return"] = 0.25
    elif change == "prices_and_return":
        rows[0].update(End_Price=150.0, Annual_Return=0.5)
    elif change == "summary_return":
        summary["Portfolio_Return"] += 0.01
        summary["Alpha"] += 0.01
    elif change == "benchmark":
        rows[1]["Benchmark_Return"] = 0.09
    elif change == "weight":
        rows[0]["Weight"] = 0.5
    elif change == "dropped_holding":
        rows = rows[:2]
    else:
        rows = []
    with pytest.raises(bt.ReconciliationError, match=message):
        bt.reconcile_year(summary, rows)
    assert bt.reconciled_years(pd.DataFrame([summary]), pd.DataFrame(rows, columns=bt.DETAILED_COLUMNS)) == set()


def test_history_rebuilds_unreconciled_years_together_and_archives_replaced_rows(
        app_modules, monkeypatch, tmp_path):
    bt = app_modules["backtest_engine"]
    provider = install_backtest_inputs(bt, monkeypatch)

    class FixedDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 13)

    monkeypatch.setattr(bt.datetime, "date", FixedDate)
    summary_path = tmp_path / "backtest_results.csv"
    source = Path(__file__).with_name("backtest_results.csv")
    summary_path.write_bytes(source.read_bytes())
    original = pd.read_csv(summary_path)
    bt.update_backtest_history(summary_path, provider=provider, end_year=2026)
    details_path = tmp_path / "backtest_detailed_results.csv"
    details = pd.read_csv(details_path)
    summary = pd.read_csv(summary_path)
    assert summary["Year"].tolist() == list(range(2016, 2026))
    assert summary["Portfolio_Return"].tolist() == pytest.approx([0.2 / 3] * 10)
    assert provider.calls == list(range(2016, 2026))
    assert details.columns.tolist() == bt.DETAILED_COLUMNS
    assert details.groupby("Year").size().to_dict() == dict.fromkeys(range(2016, 2026), 3)
    assert not details.duplicated(["Year", "Ticker"]).any()
    assert bt.reconciled_years(summary, details) == set(range(2016, 2026))
    weighted = (details["Weight"] * details["Annual_Return"]).groupby(details["Year"]).sum()
    assert weighted.tolist() == pytest.approx(summary["Portfolio_Return"].tolist(), abs=1e-9)
    archive = pd.read_csv(tmp_path / "backtest_results_archive.csv")
    pd.testing.assert_frame_equal(archive.drop(columns="Archived_At"), original)
    assert not (tmp_path / "backtest_detailed_results_archive.csv").exists()

    reconciled_summary, reconciled_details = summary_path.read_bytes(), details_path.read_bytes()
    bt.update_backtest_history(summary_path, provider=provider)
    assert details_path.read_bytes() == reconciled_details
    assert summary_path.read_bytes() == reconciled_summary
    assert len(provider.calls) == 10

    # A drifted detail year is no longer a valid decomposition of its summary row: rebuild only it.
    drifted = details.copy()
    drifted.loc[drifted["Year"] == 2020, "Annual_Return"] += 0.5
    drifted.to_csv(details_path, index=False)
    bt.update_backtest_history(summary_path, provider=provider)
    assert provider.calls[10:] == [2020]
    assert pd.read_csv(details_path).equals(details)
    assert summary_path.read_bytes() == reconciled_summary
    detail_archive = pd.read_csv(tmp_path / "backtest_detailed_results_archive.csv")
    assert detail_archive["Year"].unique().tolist() == [2020]


def test_fresh_history_generates_both_exports(app_modules, monkeypatch, tmp_path):
    bt = app_modules["backtest_engine"]
    provider = install_backtest_inputs(bt, monkeypatch)
    summary_path = tmp_path / "summary.csv"
    details_path = tmp_path / "holdings.csv"
    bt.update_backtest_history(summary_path, start_year=2024, end_year=2025,
                               provider=provider, detailed_csv_path=details_path)
    summary = pd.read_csv(summary_path)
    details = pd.read_csv(details_path)
    assert summary.columns.tolist() == [
        "Year", "Portfolio_Return", "Benchmark_Return", "Alpha", "Holdings_Returns"]
    assert summary["Year"].tolist() == [2024, 2025]
    assert summary["Holdings_Returns"].isna().all()
    assert details["Year"].tolist() == [2024] * 3 + [2025] * 3
    assert summary["Portfolio_Return"].tolist() == pytest.approx([0.2 / 3] * 2)
    with pytest.raises(ValueError, match="must be different"):
        bt.update_backtest_history(summary_path, detailed_csv_path=summary_path)


def test_failed_year_produces_header_only_detail_export(app_modules, monkeypatch, tmp_path):
    bt = app_modules["backtest_engine"]
    monkeypatch.setattr(bt, "run_pipeline", lambda *args, **kwargs: (
        0.1, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), []))
    monkeypatch.setattr(bt.DEFAULT_PROVIDER, "get_benchmark_return", lambda date: 0.1)
    bt.update_backtest_history(tmp_path / "summary.csv", start_year=2025, end_year=2025)
    details = pd.read_csv(tmp_path / "backtest_detailed_results.csv")
    assert details.empty
    assert details.columns.tolist() == bt.DETAILED_COLUMNS
    assert not (tmp_path / "summary.csv").exists()
