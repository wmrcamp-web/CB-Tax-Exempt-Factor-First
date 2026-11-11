import datetime
import json

import pandas as pd
import pytest

from selection_trace import SelectionTrace, write_selection_traces
from test_protocol_engine import BASELINE, TICKERS, install_characterization_inputs, output_fingerprint
from test_weekly_sampling import app_modules


@pytest.mark.parametrize("mod_key", ["app", "cb_taxable_portfolio_app"])
@pytest.mark.parametrize("case", ["normal", "missing", "gate_boundary", "no_survivors",
                                 "fit_error", "zero_target", "rounded_weights"])
def test_trace_preserves_primary_outputs(app_modules, monkeypatch, mod_key, case):
    mod = app_modules[mod_key]
    gate, _, _ = install_characterization_inputs(monkeypatch, mod, case)
    trace = SelectionTrace()
    result = mod.run_pipeline(datetime.date(2025, 12, 31), gate, trace=trace)
    assert len(result) == 6
    assert output_fingerprint(result) == BASELINE[case]
    assert trace.rebalance_date == "2025-12-31"
    assert trace.portfolio_year == 2026
    assert trace.gate_threshold == gate
    assert "Z-Z" in trace.constituent_universe
    assert "OIL" in trace.constituent_universe
    assert trace.screened_universe == TICKERS
    assert trace.market_cap_universe == TICKERS
    assert trace.benchmark_return == result[0]
    assert trace.final_tickers == result[3].index.tolist()
    assert set(trace.survivor_tickers) == set(result[4].index)
    if not result[3].empty:
        assert trace.weights_percent == result[3]["Weight (%)"].to_dict()
        assert trace.factor_eligible_tickers == result[2].index.tolist()
        assert trace.status == "selected"
    if case == "missing":
        assert "III" not in trace.price_eligible_tickers
    if case in ("no_survivors", "fit_error"):
        assert trace.status == "selection_failed"
        assert trace.selection_notes


def test_trace_report_merges_periods_and_serializes_nonfinite_values(tmp_path):
    path = tmp_path / "trace.json"
    older = SelectionTrace(rebalance_date="2024-12-31", portfolio_year=2025)
    newer = SelectionTrace(rebalance_date="2025-12-31", portfolio_year=2026,
                           benchmark_return=float("nan"))
    write_selection_traces([older], path)
    write_selection_traces([newer], path)
    newer.final_tickers = ["AAA"]
    write_selection_traces([newer], path)
    records = json.loads(path.read_text())
    assert len(records) == 2
    assert records[1]["final_tickers"] == ["AAA"]
    assert records[1]["benchmark_return"] is None


def test_backtest_captures_selection_and_forward_benchmark(app_modules, monkeypatch):
    app = app_modules["app"]
    bt = app_modules["backtest_engine"]
    install_characterization_inputs(monkeypatch, app, "normal")
    # This fixture customizes dashboard settings; inject that adapter explicitly.
    monkeypatch.setattr(bt, "run_pipeline", app.run_pipeline)
    monkeypatch.setattr(app.DEFAULT_PROVIDER, "get_benchmark_return", lambda date: None)
    original = app.DEFAULT_PROVIDER.get_weekly_closes

    def prices(tickers, start, end):
        if start == datetime.date(2025, 12, 31):
            return pd.DataFrame({ticker: [100.0, 110.0 if ticker == "SPY" else 120.0]
                                 for ticker in tickers})
        return original(tickers, start, end)

    monkeypatch.setattr(app.DEFAULT_PROVIDER, "get_weekly_closes", prices)
    baseline = bt.run_backtest_for_year(2026)
    traces = []
    result = bt.run_backtest_for_year(2026, traces=traces)
    assert result == baseline
    assert set(result) == {"Year", "Portfolio_Return", "Benchmark_Return", "Alpha"}
    assert len(traces) == 1
    assert traces[0].final_tickers == ["HHH", "III", "EEE", "DDD"]
    assert traces[0].gate_mode == "benchmark"
    assert traces[0].gate_threshold == traces[0].benchmark_return
    assert traces[0].forward_benchmark_return == result["Benchmark_Return"]
    assert traces[0].backtest_status == "completed"


def traced_backtest(bt, calls):
    def backtest(target_year, provider=None, traces=None):
        calls.append(target_year)
        traces.append(SelectionTrace(rebalance_date=f"{target_year - 1}-12-31",
                                     portfolio_year=target_year, final_tickers=["AAA"]))
        summary = {"Year": target_year, "Portfolio_Return": 0.2, "Benchmark_Return": 0.1,
                   "Alpha": 0.1}
        return bt.YearlyBacktest(summary, [{
            **summary, "Ticker": "AAA", "Weight": 1.0, "Weight_Percent": 100.0,
            "Start_Price": 100.0, "End_Price": 120.0, "Annual_Return": 0.2}])

    return backtest


def test_history_writes_separate_trace_only_for_new_periods(app_modules, monkeypatch, tmp_path):
    bt = app_modules["backtest_engine"]
    csv_path = tmp_path / "results.csv"
    trace_path = tmp_path / "trace.json"
    year = datetime.date.today().year - 1
    pd.DataFrame([{"Year": year - 1, "Portfolio_Return": 0.1, "Benchmark_Return": 0.1,
                   "Alpha": 0.0}]).to_csv(csv_path, index=False)
    pd.DataFrame([{"Year": year - 1, "Ticker": "AAA", "Weight": 1.0, "Weight_Percent": 100.0,
                   "Start_Price": 100.0, "End_Price": 110.0, "Annual_Return": 0.1,
                   "Portfolio_Return": 0.1, "Benchmark_Return": 0.1, "Alpha": 0.0}]).to_csv(
        tmp_path / "backtest_detailed_results.csv", index=False)
    calls = []
    monkeypatch.setattr(bt, "compute_backtest_year", traced_backtest(bt, calls))
    bt.update_backtest_history(csv_path, year - 1, trace_path=trace_path)
    assert calls == [year]
    assert json.loads(trace_path.read_text())[0]["final_tickers"] == ["AAA"]
    assert pd.read_csv(csv_path).columns.tolist() == [
        "Year", "Portfolio_Return", "Benchmark_Return", "Alpha"]


def test_missing_spy_in_selection_saves_benchmark_failure_trace(
        app_modules, monkeypatch, tmp_path):
    bt = app_modules["backtest_engine"]
    csv_path = tmp_path / "results.csv"
    trace_path = tmp_path / "trace.json"
    year = datetime.date.today().year - 1

    from test_market_data import InjectedProvider
    provider = InjectedProvider()
    provider.closes = provider.closes.drop(columns="SPY")
    monkeypatch.setattr(bt, "DEFAULT_PROVIDER", provider)
    bt.update_backtest_history(csv_path, year, trace_path=trace_path)
    row = json.loads(trace_path.read_text())[0]
    assert row["portfolio_year"] == year
    assert row["backtest_status"] == "benchmark_failed"
    assert "SPY weekly closes are missing" in row["selection_notes"][0]
    assert not csv_path.exists()


def test_trace_write_failure_does_not_block_backtest_or_summary_csv(
        app_modules, monkeypatch, tmp_path, capsys):
    bt = app_modules["backtest_engine"]
    csv_path = tmp_path / "results.csv"
    trace_path = tmp_path / "trace.json"
    trace_path.write_text("invalid JSON")
    year = datetime.date.today().year - 1
    calls = []
    monkeypatch.setattr(bt, "compute_backtest_year", traced_backtest(bt, calls))
    bt.update_backtest_history(csv_path, year - 1, trace_path=trace_path)

    assert calls == [year - 1, year]
    assert pd.read_csv(csv_path)["Year"].tolist() == [year - 1, year]
    assert trace_path.read_text() == "invalid JSON"
    assert "Error writing selection traces" in capsys.readouterr().out
