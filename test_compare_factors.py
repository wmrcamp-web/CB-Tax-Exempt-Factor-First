from types import SimpleNamespace
from datetime import date

import pandas as pd
import pytest

import backtest_engine
import compare_factors as cf
from backtest_engine import YearlyBacktest
from data_errors import MarketDataError
from selection_trace import SelectionTrace


def test_factor_argument_reaches_selection(monkeypatch):
    captured = []

    def pipeline(date, gate, provider, **kwargs):
        captured.append(kwargs)
        return None, None, None, pd.DataFrame(), None, None

    monkeypatch.setattr(backtest_engine, "run_pipeline", pipeline)
    backtest_engine.compute_backtest_year(2020, provider=object())
    backtest_engine.compute_backtest_year(2020, provider=object(), n_factors=13)
    assert "n_factors" not in captured[0]
    assert captured[1]["n_factors"] == 13
    with pytest.raises(ValueError):
        backtest_engine.compute_backtest_year(2020, provider=object(), n_factors=0)


def test_paired_provider_freezes_selection_inputs():
    class Base:
        notes = ["historical"]
        forward_notes = {}
        calls = []

        def get_constituents(self, as_of):
            self.calls.append("members")
            return pd.DataFrame({"Symbol": ["A"]})

        def get_market_caps(self, tickers, as_of):
            self.calls.append("caps")
            return {"A": 10}

        def get_weekly_closes(self, tickers, start, end):
            self.calls.append("prices")
            return pd.DataFrame({"A": [1.0]})

    base = Base()
    paired = cf._PairedProvider(base)
    day = date(2020, 12, 31)
    constituents = paired.get_constituents(day)
    constituents.loc[0, "Symbol"] = "MODIFIED"
    paired.get_market_caps(["A"], day)["A"] = 0
    prices = paired.get_weekly_closes(["A"], day, day)
    prices.iloc[0, 0] = 0
    assert paired.get_constituents(day).loc[0, "Symbol"] == "A"
    assert paired.get_market_caps(["A"], day)["A"] == 10
    assert paired.get_weekly_closes(["A"], day, day).iloc[0, 0] == 1
    assert base.calls == ["members", "caps", "prices"]


def prices_provider(missing=False):
    dates = pd.to_datetime(["2019-12-31", "2020-06-01", "2020-09-01", "2020-12-31"])
    frame = pd.DataFrame(
        {"SPY": [100, 110, 90, 110],
         "A": [100, None if missing else 120, 80, 110],
         "B": [100, 80, None, None]}, index=dates,
    )
    return SimpleNamespace(_daily_closes=lambda tickers, start, end: frame)


def daily_result():
    return YearlyBacktest(
        {"Portfolio_Return": -0.05},
        [{"Ticker": "A", "Weight": 0.5, "Start_Price": 100, "End_Price": 110,
          "Exit_Valuation": "Observed annual endpoint"},
         {"Ticker": "B", "Weight": 0.5, "Start_Price": 100, "End_Price": 80,
          "Exit_Valuation": "ESTIMATE: liquidation"}],
    )


def test_daily_drawdown_values_hold_estimated_exit_in_cash():
    values = cf._daily_values(prices_provider(), 2020, daily_result())
    assert list(values.values()) == pytest.approx([1.0, 1.0, 0.8, 0.95])
    assert cf._max_drawdown(values.values()) == pytest.approx(-0.2)


def test_missing_intermediate_close_and_endpoint_mismatch_fail():
    with pytest.raises(MarketDataError, match="missing daily close"):
        cf._daily_values(prices_provider(missing=True), 2020, daily_result())
    result = daily_result()
    result.details[0]["End_Price"] = 111
    with pytest.raises(MarketDataError, match="endpoints differ"):
        cf._daily_values(prices_provider(), 2020, result)


def test_full_period_drawdown_carries_previous_peak_across_rebalance():
    years = [
        {"8_factors": {"daily_values": {"a": 1, "b": 1.5, "c": 1.2},
                       "summary": {"Portfolio_Return": 0.2, "Benchmark_Return": 0.1}}},
        {"8_factors": {"daily_values": {"c": 1, "d": 0.75, "e": 1.0},
                       "summary": {"Portfolio_Return": 0.0, "Benchmark_Return": 0.0}}},
    ]
    stats = cf._full_period(years, "8_factors")
    assert stats["max_drawdown"] == pytest.approx(-0.4)
    assert stats["annualized_return"] == pytest.approx(1.2 ** 0.5 - 1)
    assert stats["annualized_excess_return"] == pytest.approx(1.2 ** 0.5 - 1.1 ** 0.5)


def test_incomplete_arm_withholds_full_period_and_preserves_report(tmp_path, monkeypatch):
    calls = []

    def arm(provider, year, factors):
        calls.append((year, factors))
        trace = SelectionTrace(
            gate_mode="benchmark", gate_threshold=0.1,
            benchmark_window_start="start", benchmark_window_end="end",
            price_eligible_tickers=[str(i) for i in range(20)],
            backtest_status="completed",
        )
        return {"status": "failed" if year == 2021 and factors == 13 else "complete",
                "error": "missing daily prices" if year == 2021 and factors == 13 else "",
                "trace": vars(trace), "summary": {"Portfolio_Return": 0.1,
                "Benchmark_Return": 0.05, "Alpha": 0.05},
                "holdings": [], "daily_values": {f"{year - 1}-12-31": 1, f"{year}-12-31": 1.1},
                "max_drawdown": 0}

    monkeypatch.setattr(cf, "_arm", arm)
    path = tmp_path / "report.json"
    report = cf.run_comparison(SimpleNamespace(sector_source="sharadar",
                               delisting_policy="last_close_cash"), [2020, 2021], path,
                               progress=lambda _: None)
    assert calls == [(2020, 8), (2020, 13), (2021, 8), (2021, 13)]
    assert report["years"][0]["status"] == "complete"
    assert report["years"][1]["status"] == "failed"
    assert "full_period" not in report
    assert "missing daily prices" in path.read_text()


def test_different_selection_universe_withholds_pair():
    trace = {"gate_mode": "benchmark", "gate_threshold": 0.1,
             "benchmark_window_start": "a", "benchmark_window_end": "b",
             "constituent_universe": ["A"], "screened_universe": ["A"],
             "market_cap_universe": ["A"], "price_eligible_tickers": ["A"],
             "survivor_tickers": ["A"]}
    a = {"trace": trace, "summary": {"Benchmark_Return": 0.1}, "daily_values": {"a": 1}}
    b = {"trace": {**trace, "constituent_universe": ["B"]},
         "summary": {"Benchmark_Return": 0.1}, "daily_values": {"a": 1}}
    with pytest.raises(MarketDataError, match="constituent_universe"):
        cf._check_pair(a, b)
