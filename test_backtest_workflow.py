"""Real Streamlit button/rerun workflows with deterministic, offline providers."""

import datetime
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import subprocess
import sys
from threading import Barrier

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import backtest_engine as bt
import backtest_exports as exports
from test_weekly_sampling import FakeFactorAnalyzer, fake_sp500_df, make_weekly_closes


ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "pages" / "1_Backtest_Results.py"


def detail_frame(years, portfolio=0.1, benchmark=0.08):
    return pd.DataFrame([{
        "Year": year, "Ticker": "SAVED", "Weight": 1.0, "Weight_Percent": 100.0,
        "Start_Price": 100.0, "End_Price": 100.0 * (1 + portfolio), "Annual_Return": portfolio,
        "Portfolio_Return": portfolio, "Benchmark_Return": benchmark, "Alpha": portfolio - benchmark,
    } for year in years], columns=bt.DETAILED_COLUMNS)


def saved_detail_frame(years):
    """Saved holdings that exactly decompose the fixture's first summary year (0.17 / 0.08)."""
    return detail_frame(years, portfolio=0.17, benchmark=0.08)


def assert_summary_rebuilt(directory, summary, original, rebuilt_years):
    """Rebuilt years carry the same run's returns; others are unchanged; replaced rows archived."""
    current = pd.read_csv(summary).set_index("Year")
    before = pd.read_csv(io.BytesIO(original))
    before["Year"] = before["Year"].astype(int)
    before = before.set_index("Year")
    assert current.index.tolist() == before.index.tolist()
    for year in before.index:
        expected = (0.2, 0.08) if year in rebuilt_years else tuple(
            before.loc[year, ["Portfolio_Return", "Benchmark_Return"]])
        assert tuple(current.loc[year, ["Portfolio_Return", "Benchmark_Return"]]) == pytest.approx(expected)
    archive = pd.read_csv(directory / "backtest_results_archive.csv")
    assert sorted(archive["Year"].astype(int)) == sorted(rebuilt_years)
    assert archive["Archived_At"].notna().all()


@pytest.fixture
def workflow(monkeypatch, tmp_path):
    monkeypatch.setattr("backtest_results_page.require_access", lambda: None)
    monkeypatch.setattr(exports, "DATA_DIR", tmp_path)
    monkeypatch.setattr("protocol_engine.FactorAnalyzer", FakeFactorAnalyzer)
    current = datetime.date.today().year
    years = [current - 2, current - 1]
    summary = tmp_path / "backtest_results.csv"
    # Preserve original bytes, including CRLF, noncanonical year formatting and an incomplete row.
    summary.write_bytes((
        "Year,Portfolio_Return,Benchmark_Return,Alpha,Holdings_Returns\r\n"
        f"{years[0]}.0,0.17,0.08,0.09,\r\n"
        f"{years[1]},0.23,0.09,0.14,\r\n"
        f"{current},0.99,0.10,0.89,\r\n"
    ).encode())
    original = summary.read_bytes()

    class Provider:
        calls = []
        failed_years = set()

        def get_benchmark_return(self, date):
            raise AssertionError("Default gate must use the selection run's SPY window")

        def get_constituents(self, date):
            self.calls.append(("selection", date.year + 1))
            return fake_sp500_df([f"FAKE{i}" for i in range(12)])

        def get_market_caps(self, tickers, date):
            return {ticker: (100 - i) * 1e9 for i, ticker in enumerate(tickers)}

        def get_weekly_closes(self, tickers, start, end):
            self.calls.append(("prices", start, end))
            if (end - start).days == 742:
                self.calls.append(("benchmark", end.year + 1))
                closes = make_weekly_closes([t for t in tickers if t != "SPY"], end=end)
                if end.year + 1 in self.failed_years:
                    closes = closes.drop(columns="SPY")
                return closes
            return pd.DataFrame({ticker: [100.0, 108.0 if ticker == "SPY" else 120.0]
                                 for ticker in tickers})

    provider = Provider()
    monkeypatch.setattr(bt, "DEFAULT_PROVIDER", provider)
    downloads = {}
    original_download = st.download_button

    def capture(label, **kwargs):
        downloads[kwargs["file_name"]] = (label, kwargs["data"])
        return original_download(label, **kwargs)

    monkeypatch.setattr(st, "download_button", capture)
    return tmp_path, years, summary, original, provider, downloads


def run_page():
    page = AppTest.from_file(str(PAGE)).run(timeout=20)
    assert not page.exception
    return page


def test_button_rebuilds_summary_and_details_from_one_run(workflow, monkeypatch):
    directory, years, summary, original, provider, downloads = workflow
    monkeypatch.chdir("/tmp")
    page = run_page()
    assert not provider.calls  # Render and summary download do no provider work.
    assert len(page.get("download_button")) == 1
    assert downloads["backtest_results.csv"][1] == original
    assert not (directory / "backtest_detailed_results.csv").exists()
    page.button[0].click().run(timeout=20)
    assert not page.exception
    assert [call[1] for call in provider.calls if call[0] == "benchmark"] == years
    label, payload = downloads["backtest_detailed_results.csv"]
    assert "partial" not in label
    details = pd.read_csv(io.BytesIO(payload))
    assert set(details["Year"]) == set(years)
    assert details["Ticker"].str.startswith("FAKE").all()
    assert details["Annual_Return"].tolist() == pytest.approx([0.2] * len(details))
    assert details.groupby("Year")["Weight"].sum().tolist() == pytest.approx([1.0, 1.0])
    assert details["Detail_Source"].str.startswith("Generated with its annual summary").all()
    pd.testing.assert_frame_equal(page.dataframe[1].value, details)
    assert page.success
    assert_summary_rebuilt(directory, summary, original, years)
    rebuilt = summary.read_bytes()
    assert downloads["backtest_results.csv"][1] == rebuilt
    exported = pd.read_csv(io.BytesIO(rebuilt)).set_index("Year").loc[years]
    weighted = (details["Weight"] * details["Annual_Return"]).groupby(details["Year"]).sum()
    assert weighted.tolist() == pytest.approx(exported["Portfolio_Return"].tolist(), abs=1e-9)
    for column in ("Portfolio_Return", "Benchmark_Return", "Alpha"):
        assert (details.groupby("Year")[column].nunique() == 1).all()
        assert details.groupby("Year")[column].first().tolist() == pytest.approx(
            exported[column].tolist(), abs=1e-9)
    calls = list(provider.calls)
    page.run()
    assert not page.exception
    assert provider.calls == calls
    assert downloads["backtest_detailed_results.csv"][1] == payload
    assert downloads["backtest_results.csv"][1] == rebuilt
    assert summary.read_bytes() == rebuilt


@pytest.mark.parametrize("contents", [
    None, b"", b"Year,Ticker\n", b"Ticker,Weight\nAAA,1\n",
    b'Year,Ticker\n"unterminated', b"Year,Ticker\n2024,A,extra\n",
])
def test_absent_empty_malformed_details_recover_from_button(workflow, contents):
    directory, years, summary, original, provider, downloads = workflow
    path = directory / "backtest_detailed_results.csv"
    if contents is not None:
        path.write_bytes(contents)
    page = run_page()
    assert not page.success
    assert len(page.get("download_button")) == 1
    assert not provider.calls
    page.button[0].click().run(timeout=20)
    assert not page.exception
    assert page.success
    assert set(pd.read_csv(path)["Year"]) == set(years)
    assert_summary_rebuilt(directory, summary, original, years)


def test_partial_details_retry_failures_and_keep_saved_holdings(workflow):
    directory, years, summary, original, provider, downloads = workflow
    saved = saved_detail_frame([years[0], datetime.date.today().year])
    saved.to_csv(directory / "backtest_detailed_results.csv", index=False)
    provider.failed_years.add(years[1])
    page = run_page()
    assert "partial" in downloads["backtest_detailed_results.csv"][0]
    page.button[0].click().run(timeout=20)
    assert not page.exception
    assert not page.success
    assert [call[1] for call in provider.calls if call[0] == "benchmark"] == [years[1]]
    assert any("SPY weekly closes are missing" in item.value for item in page.warning)
    details = pd.read_csv(io.BytesIO(downloads["backtest_detailed_results.csv"][1]))
    assert details["Year"].tolist() == [years[0]]
    assert details["Ticker"].tolist() == ["SAVED"]
    page.run()
    assert any("SPY weekly closes are missing" in item.value for item in page.warning)
    provider.failed_years.clear()
    page.button[0].click().run(timeout=20)
    assert not page.exception
    assert page.success
    details = pd.read_csv(io.BytesIO(downloads["backtest_detailed_results.csv"][1]))
    assert details.loc[details["Year"] == years[0], "Ticker"].tolist() == ["SAVED"]
    assert set(details["Year"]) == set(years)
    assert_summary_rebuilt(directory, summary, original, [years[1]])


def test_readonly_fallback_keeps_successes_across_reruns_and_retry(workflow, monkeypatch):
    directory, years, summary, original, provider, downloads = workflow
    path = directory / "backtest_detailed_results.csv"
    saved_detail_frame([years[0]]).to_csv(path, index=False)
    original_details = path.read_bytes()

    def deny_replace(*args):
        raise PermissionError("read-only deployment (mock)")

    monkeypatch.setattr(bt.os, "replace", deny_replace)
    page = run_page()
    page.button[0].click().run(timeout=20)
    assert not page.exception
    assert page.success
    assert any("Local persistence is unavailable" in item.value for item in page.warning)
    payload = downloads["backtest_detailed_results.csv"][1]
    session_summary = pd.read_csv(io.BytesIO(downloads["backtest_results.csv"][1])).set_index("Year")
    assert session_summary.loc[years, "Portfolio_Return"].tolist() == pytest.approx([0.17, 0.2])
    calls = list(provider.calls)
    page.run()
    assert not page.exception
    assert provider.calls == calls
    assert downloads["backtest_detailed_results.csv"][1] == payload
    assert pd.read_csv(io.BytesIO(downloads["backtest_results.csv"][1])).set_index("Year").equals(
        session_summary)
    assert any("session ends" in item.value for item in page.warning)
    assert path.read_bytes() == original_details
    assert summary.read_bytes() == original
    assert sorted(item.name for item in directory.iterdir()) == [
        "backtest_detailed_results.csv", "backtest_results.csv"]


def test_failure_does_not_discard_successful_generated_year(workflow):
    directory, years, summary, original, provider, downloads = workflow
    provider.failed_years.add(years[0])
    page = run_page()
    page.button[0].click().run(timeout=20)
    assert not page.exception
    assert not page.success
    assert "partial" in downloads["backtest_detailed_results.csv"][0]
    assert pd.read_csv(directory / "backtest_detailed_results.csv")["Year"].unique().tolist() == [years[1]]
    assert_summary_rebuilt(directory, summary, original, [years[1]])
    calls = list(provider.calls)
    page.run()
    assert provider.calls == calls
    provider.failed_years.clear()
    page.button[0].click().run(timeout=20)
    assert page.success
    assert [call[1] for call in provider.calls[len(calls):] if call[0] == "benchmark"] == [years[0]]
    assert_summary_rebuilt(directory, summary, original, years)


def test_drifted_saved_details_are_not_published_until_rebuilt(workflow):
    directory, years, summary, original, provider, downloads = workflow
    path = directory / "backtest_detailed_results.csv"
    detail_frame([years[0]]).to_csv(path, index=False)  # 10% holdings vs. 17% saved summary.
    original_details = path.read_bytes()
    page = run_page()
    assert not provider.calls
    assert not page.success
    assert "backtest_detailed_results.csv" not in downloads
    assert any("do not reconcile" in item.value and "differs from the annual summary" in item.value
               for item in page.warning)
    assert summary.read_bytes() == original
    page.button[0].click().run(timeout=20)
    assert not page.exception
    assert page.success
    assert [call[1] for call in provider.calls if call[0] == "benchmark"] == years
    details = pd.read_csv(path)
    assert "SAVED" not in details["Ticker"].tolist()
    assert_summary_rebuilt(directory, summary, original, years)
    archived = pd.read_csv(directory / "backtest_detailed_results_archive.csv")
    assert archived.drop(columns=["Detail_Source", "Archived_At"]).equals(
        pd.read_csv(io.BytesIO(original_details)))


def test_saved_reconciled_details_are_published_without_rebuild(workflow):
    directory, years, summary, original, provider, downloads = workflow
    frame = pd.concat([saved_detail_frame([years[0]]),
                       detail_frame([years[1]], portfolio=0.23, benchmark=0.09)])
    frame.to_csv(directory / "backtest_detailed_results.csv", index=False)
    page = run_page()
    assert not provider.calls
    assert not page.button
    assert page.success
    assert set(pd.read_csv(io.BytesIO(downloads["backtest_detailed_results.csv"][1]))["Year"]) == set(years)
    assert summary.read_bytes() == original


def test_all_failed_years_have_no_detail_download_or_success(workflow):
    directory, years, summary, original, provider, downloads = workflow
    provider.failed_years.update(years)
    page = run_page()
    page.button[0].click().run(timeout=20)
    assert not page.exception
    assert not page.success
    assert len(page.get("download_button")) == 1
    assert not (directory / "backtest_detailed_results.csv").exists()
    assert summary.read_bytes() == original


@pytest.mark.parametrize("contents", [
    None, b"", b"Year,Portfolio_Return,Benchmark_Return\n",
    b"Year,Portfolio_Return,Benchmark_Return\ninvalid,0.1,0.2\n",
    b"Year,Portfolio_Return,Benchmark_Return\n2025.5,0.1,0.2\n",
    b"Year,Portfolio_Return,Benchmark_Return\n2025,inf,0.2\n",
    b"Ticker\nAAA\n", b'Year\n"unterminated',
])
def test_no_valid_summary_never_generates_or_downloads(workflow, contents):
    directory, years, summary, original, provider, downloads = workflow
    if contents is None:
        summary.unlink()
    else:
        summary.write_bytes(contents)
    page = run_page()
    assert page.warning
    assert not page.button
    assert not page.get("download_button")
    assert not provider.calls
    if contents is not None:
        assert summary.read_bytes() == contents


@pytest.mark.parametrize("change", ["fractional_year", "blank_ticker", "weights", "all_missing", "duplicate", "missing_column"])
def test_invalid_detail_groups_are_not_full_coverage(change):
    frame = detail_frame([2024])
    if change == "fractional_year":
        frame["Year"] = 2024.5
    elif change == "blank_ticker":
        frame["Ticker"] = " "
    elif change == "weights":
        frame["Weight"] = 0.5
    elif change == "all_missing":
        frame["Annual_Return"] = float("nan")
    elif change == "duplicate":
        frame = pd.concat([frame, frame])
    else:
        frame = frame.drop(columns="Start_Price")
    assert exports.validate_details(frame).empty


def test_engine_import_has_no_dashboard_ui_or_network(tmp_path):
    code = """
import sys
from unittest.mock import patch
with patch("streamlit.set_page_config", side_effect=AssertionError("dashboard config")), \\
     patch("streamlit.title", side_effect=AssertionError("dashboard title")), \\
     patch("yfinance.download", side_effect=AssertionError("unsolicited network")):
    import backtest_engine
    import backtest_results_page
    assert "app" not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_default_paths_are_module_relative(monkeypatch):
    monkeypatch.chdir("/tmp")
    assert exports.DATA_DIR == ROOT
    assert bt.update_backtest_history.__defaults__[0] == ROOT / "backtest_results.csv"
    source, summary = exports.read_summary(exports.DATA_DIR / "backtest_results.csv")
    assert source
    assert not summary.empty


def test_concurrent_sessions_merge_successful_years_instead_of_overwriting(tmp_path):
    path = tmp_path / "backtest_detailed_results.csv"
    barrier = Barrier(2)

    def save(year):
        # Both sessions started from the same absent file, each with one success.
        frame = exports.validate_details(detail_frame([year]))
        barrier.wait(timeout=5)
        return exports.persist_details(frame, path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [2024, 2025]))
    assert set(pd.read_csv(path)["Year"]) == {2024, 2025}
    assert max(len(frame) for frame in results) == 2
