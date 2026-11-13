import copy
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

import backtest_engine
import compare_shadow as cs
import protocol_engine
from data_errors import MarketDataError
from test_protocol_engine import selection_inputs


def capped(rows, shadows, target=16):
    metrics, loadings, headers = selection_inputs(rows)
    return protocol_engine.assemble_portfolio(
        0.1, metrics, loadings, pd.DataFrame(), headers, shadows, target,
        shadow_fraction_limit=0.25,
    )[3]


def test_cap_uses_actual_portfolio_size_and_preserves_factor_caps():
    rows = [
        ("S1", 0.1, 0.1, 0.3, 0.1),
        ("S2", 0.1, 0.2, 0.3, 0.1),
        ("S3", 0.1, 0.3, 0.3, 0.1),
        ("S4", 0.1, 0.4, 0.1, 0.3),
        ("N1", 0.3, 0.5, 0.7, 0.1),
        ("N2", 0.2, 0.6, 0.7, 0.1),
        ("N3", 0.1, 0.7, 0.1, 0.7),
        ("N4", 0.1, 0.8, 0.1, 0.7),
        ("N5", 0.1, 0.9, 0.1, 0.7),
    ]
    portfolio = capped(rows, ["S1", "S2", "S3", "S4"])
    assert len(portfolio) == 6
    assert portfolio.index.tolist() == ["S1", "N1", "N3", "N2", "N4", "N5"]
    assert portfolio["In_Shadow_List"].sum() == 1
    assert portfolio["Dominant_Factor"].value_counts().max() == 3


def test_cap_does_not_add_shadow_in_final_fill():
    rows = [(f"S{i}", 0.1, i / 10, 0.3 if i < 3 else 0.1, 0.1 if i < 3 else 0.3)
            for i in range(5)]
    rows += [(f"N{i}", 0.1, 0.5, 0.7 if i < 3 else 0.1, 0.1 if i < 3 else 0.7)
             for i in range(5)]
    portfolio = capped(rows, [f"S{i}" for i in range(5)], target=4)
    assert len(portfolio) == 4
    assert portfolio["In_Shadow_List"].sum() == 1
    assert portfolio.index[0] == "S0"


def test_fewer_than_four_holdings_cannot_include_shadow():
    portfolio = capped([("S", 0.1, 0.1, 0.3, 0.1),
                        ("N", 0.1, 0.2, 0.1, 0.7)], ["S"])
    assert portfolio.index.tolist() == ["N"]


def test_full_portfolio_has_no_more_than_four_shadows():
    headers = [f"Factor_{i}" for i in range(1, 14)]
    tickers = [f"S{i}" for i in range(6)] + [f"N{i}" for i in range(13)]
    loadings = pd.DataFrame(0.0, index=tickers, columns=headers)
    for i, ticker in enumerate(tickers):
        loadings.loc[ticker, headers[i % len(headers)]] = 0.3 if ticker.startswith("S") else 0.7
    metrics = pd.DataFrame({
        "T12M_Return": [0.1] * len(tickers),
        "Downside_Beta": [i / 100 for i in range(len(tickers))],
        "Max_Factor_Loading": loadings.abs().max(axis=1),
        "Dominant_Factor": loadings.abs().idxmax(axis=1),
        "Sector": ["Technology"] * len(tickers),
    }, index=tickers)
    portfolio = protocol_engine.assemble_portfolio(
        0.1, metrics, loadings, pd.DataFrame(), headers, tickers[:6], 16,
        shadow_fraction_limit=0.25,
    )[3]
    assert len(portfolio) == 16
    assert portfolio["In_Shadow_List"].sum() == 4
    assert portfolio.index[:4].tolist() == tickers[:4]


def test_shadow_option_propagates_to_pipeline(monkeypatch):
    calls = []

    def pipeline(date, gate, provider, **kwargs):
        calls.append(kwargs)
        return None, None, None, pd.DataFrame(), None, None

    monkeypatch.setattr(backtest_engine, "run_pipeline", pipeline)
    backtest_engine.compute_backtest_year(2020, object(), n_factors=13, shadow_fraction_limit=0.25)
    assert calls[0]["shadow_fraction_limit"] == 0.25
    assert calls[0]["n_factors"] == 13


def arm(year):
    trace = {"final_tickers": ["A"], "constituent_universe": ["A"], "screened_universe": ["A"],
             "market_cap_universe": ["A"], "price_eligible_tickers": ["A"],
             "survivor_tickers": ["A"], "factor_eligible_tickers": ["A"],
             "benchmark_window_start": "start", "benchmark_window_end": "end",
             "gate_mode": "benchmark", "gate_threshold": 0.1}
    return {"status": "complete", "trace": trace, "holdings": [{"Ticker": "A", "In_Shadow_List": False}],
            "summary": {"Portfolio_Return": 0.1, "Benchmark_Return": 0.05, "Alpha": 0.05},
            "daily_values": {f"{year - 1}-12-31": 1.0, f"{year}-12-31": 1.1},
            "max_drawdown": 0.0}


def test_previous_mismatch_withholds_year_and_full_period(tmp_path, monkeypatch):
    baseline = {
        "requested_years": [2020], "factor_counts": [8, 13], "sector_source": "sharadar",
        "delisting_policy": "last_close_cash", "excluded_symbols": sorted(cs.EXCLUDED_SYMBOLS),
        "source_sha256": {name: hashlib.sha256((cs.ROOT / name).read_bytes()).hexdigest()
                          for name in cs.UNCHANGED_FILES},
        "years": [{"year": 2020, "status": "complete", "13_factors": arm(2020)}],
    }
    baseline["years"][0]["13_factors"]["summary"]["Portfolio_Return"] = 0.2
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline))
    monkeypatch.setattr(cs, "_arm", lambda *args, **kwargs: copy.deepcopy(arm(2020)))
    output = tmp_path / "result.json"
    report = cs.run_comparison(SimpleNamespace(sector_source="sharadar",
                               delisting_policy="last_close_cash"), [2020], path, output,
                               progress=lambda _: None)
    assert report["years"][0]["status"] == "failed"
    assert "differs from the previous report" in report["years"][0]["error"]
    assert "full_period" not in report
    assert json.loads(output.read_text()) == report


def test_comparison_refuses_mismatched_policy(tmp_path):
    provider = SimpleNamespace(sector_source="sharadar", delisting_policy="strict")
    with pytest.raises(MarketDataError, match="different years or settings"):
        cs._verify_baseline({"requested_years": [2020]}, provider, [2020])


def test_complete_comparison_records_cap_and_original_report(tmp_path, monkeypatch):
    baseline = {
        "requested_years": [2020], "factor_counts": [8, 13], "sector_source": "sharadar",
        "delisting_policy": "last_close_cash", "excluded_symbols": sorted(cs.EXCLUDED_SYMBOLS),
        "source_sha256": {name: hashlib.sha256((cs.ROOT / name).read_bytes()).hexdigest()
                          for name in cs.UNCHANGED_FILES},
        "years": [{"year": 2020, "status": "complete", "13_factors": arm(2020)}],
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))
    before = baseline_path.read_bytes()
    calls = []

    def fake_arm(provider, year, factors, **kwargs):
        calls.append((factors, kwargs.get("shadow_fraction_limit")))
        return copy.deepcopy(arm(year))

    monkeypatch.setattr(cs, "_arm", fake_arm)
    report = cs.run_comparison(SimpleNamespace(sector_source="sharadar",
                               delisting_policy="last_close_cash"), [2020],
                               baseline_path, tmp_path / "new.json", progress=lambda _: None)
    assert calls == [(13, None), (13, 0.25)]
    assert report["years"][0]["shadow_count"] == 0
    assert report["years"][0]["return_difference"] == 0
    assert report["full_period"]["capped"]["annualized_return"] == pytest.approx(0.1)
    assert baseline_path.read_bytes() == before
