"""Market-cap acquisition/ranking and benchmark-gate consistency (all network mocked)."""

import datetime
import math
from pathlib import Path
from time import sleep as real_sleep

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import data_fetcher
import protocol_engine as engine
from data_errors import BenchmarkError, MarketCapError, MarketDataError, describe_error
from portfolio_pipeline import BENCHMARK_GATE
from selection_trace import SelectionTrace
from test_protocol_engine import TICKERS, install_characterization_inputs, output_fingerprint
from test_weekly_sampling import (
    FakeFactorAnalyzer, app_modules, fake_sp500_df, make_weekly_closes, to_yf_frame,
)


ROOT = Path(__file__).resolve().parent
INVALID_CAPS = [0, 0.0, -5e9, float("nan"), float("inf"), float("-inf"), None, "n/a", True]


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    sleeps = []
    monkeypatch.setattr(data_fetcher.time, "sleep", sleeps.append)
    data_fetcher.clear_market_data_caches()
    yield sleeps
    data_fetcher.clear_market_data_caches()


class FakeYahoo:
    """Per-ticker sequences of info payloads or exceptions for yf.Ticker(t).info."""

    def __init__(self, behaviors, delay=0.0):
        self.behaviors = {t: list(v) for t, v in behaviors.items()}
        self.calls = []
        self.delay = delay

    def __call__(self, ticker):
        fake = self

        class Ticker:
            @property
            def info(self):
                fake.calls.append(ticker)
                real_sleep(fake.delay)
                queue = fake.behaviors[ticker]
                outcome = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        return Ticker()


def install_yahoo(monkeypatch, behaviors, delay=0.0):
    fake = FakeYahoo(behaviors, delay)
    monkeypatch.setattr(data_fetcher.yf, "Ticker", fake)
    return fake


# ---------------------------------------------------------------------------
# Live market-cap fetching
# ---------------------------------------------------------------------------

def test_all_failed_fetches_warn_assign_zero_and_refresh_recovers(monkeypatch, fresh_caches, caplog):
    tickers = ["AAA", "BBB", "CCC"]
    fake = install_yahoo(monkeypatch, {t: [ConnectionError("down?token=SECRET")] for t in tickers})

    ordered, caps = data_fetcher.get_sorted_by_market_cap(tickers)
    assert ordered == tickers
    assert caps == dict.fromkeys(tickers, 0.0)
    message = caplog.text
    assert "0/3 valid" in message and "ConnectionError: 3" in message
    assert "SECRET" not in message and "token" not in message
    assert fake.calls.count("AAA") == data_fetcher.MARKET_CAP_ATTEMPTS
    assert sorted(fresh_caches) == sorted([1, 2] * 3)

    # Zero placeholders are cached; explicit refresh retries the live source.
    fake.behaviors = {t: [{"marketCap": cap}] for t, cap in zip(tickers, [1e9, 3e9, 2e9])}
    assert data_fetcher.get_sorted_by_market_cap(tickers)[1] == caps
    data_fetcher.clear_market_data_caches()
    assert data_fetcher.get_sorted_by_market_cap(tickers)[0] == ["BBB", "CCC", "AAA"]


def test_transient_exception_reaches_retry_and_recovers(monkeypatch, fresh_caches):
    fake = install_yahoo(monkeypatch, {
        "AAA": [TimeoutError("slow"), RuntimeError("rate limited"), {"marketCap": 5e9}],
        "BBB": [{"marketCap": 7e9}],
    })
    ordered, caps = data_fetcher.get_sorted_by_market_cap(["AAA", "BBB"])
    assert ordered == ["BBB", "AAA"]
    assert caps == {"AAA": 5e9, "BBB": 7e9}
    assert fake.calls.count("AAA") == 3
    assert fresh_caches == [1, 2]


@pytest.mark.parametrize("raw", INVALID_CAPS)
def test_missing_or_invalid_yahoo_caps_are_zero_and_rank_last(monkeypatch, raw, caplog):
    info = {} if raw is None else {"marketCap": raw}
    install_yahoo(monkeypatch, {"AAA": [{"marketCap": 2e9}], "BAD": [info]})
    ordered, caps = data_fetcher.get_sorted_by_market_cap(["BAD", "AAA"])
    assert ordered == ["AAA", "BAD"]
    assert caps == {"AAA": 2e9, "BAD": 0.0}
    assert "Assigned zero and ranked last: BAD" in caplog.text


def test_real_caps_rank_deterministically(monkeypatch):
    caps = {"LOW": 1e9, "HIGH": 9e11, "MID": 5e10, "TIE1": 2e10, "TIE2": 2e10}
    install_yahoo(monkeypatch, {t: [{"marketCap": int(c)}] for t, c in caps.items()})
    ordered, fetched = data_fetcher.get_sorted_by_market_cap(list(caps))
    assert ordered == ["HIGH", "MID", "TIE1", "TIE2", "LOW"]
    assert fetched == caps and all(isinstance(v, float) for v in fetched.values())


def test_incomplete_coverage_ranks_unknowns_last_without_dropping(monkeypatch):
    behaviors = {f"T{i}": [{"marketCap": (i + 1) * 1e9}] for i in range(30)}
    behaviors["T7"] = [{"quoteType": "EQUITY"}]
    install_yahoo(monkeypatch, behaviors)
    ordered, caps = data_fetcher.get_sorted_by_market_cap(list(behaviors))
    assert len(ordered) == 30 and ordered[-1] == "T7" and caps["T7"] == 0


def test_mass_failure_attempts_every_ticker_before_ranking(monkeypatch):
    monkeypatch.setattr(data_fetcher, "MARKET_CAP_MAX_WORKERS", 1)
    tickers = [f"T{i}" for i in range(100)]
    fake = install_yahoo(monkeypatch, {t: [ConnectionError("blocked")] for t in tickers},
                         delay=0.005)
    assert data_fetcher.fetch_market_caps(tickers) == dict.fromkeys(tickers, 0.0)
    assert set(fake.calls) == set(tickers)
    assert all(fake.calls.count(t) == data_fetcher.MARKET_CAP_ATTEMPTS for t in tickers)


def test_refresh_invalidates_market_data_caches_only(monkeypatch):
    fake = install_yahoo(monkeypatch, {"AAA": [{"marketCap": 1e9}]})
    assert data_fetcher.get_sorted_by_market_cap(["AAA"])[1] == {"AAA": 1e9}
    fake.behaviors = {"AAA": [{"marketCap": 4e9}]}
    assert data_fetcher.get_sorted_by_market_cap(["AAA"])[1] == {"AAA": 1e9}  # cached
    data_fetcher.clear_market_data_caches()
    assert data_fetcher.get_sorted_by_market_cap(["AAA"])[1] == {"AAA": 4e9}


def test_error_descriptions_redact_url_queries():
    exc = RuntimeError("GET https://query2.finance.yahoo.com/v10?crumb=abc&cookie=xyz failed")
    text = describe_error(exc)
    assert text.startswith("RuntimeError: ") and "crumb" not in text and "xyz" not in text


# ---------------------------------------------------------------------------
# Benchmark fetch never becomes 0%
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("frame", [
    to_yf_frame(pd.DataFrame({"SPY": []}, dtype=float)),
    to_yf_frame(pd.DataFrame({"SPY": [np.nan, np.nan]})),
])
def test_benchmark_fetch_failure_is_not_zero(monkeypatch, fresh_caches, frame):
    monkeypatch.setattr("yfinance.download", lambda *args, **kwargs: frame)
    with pytest.raises(BenchmarkError, match="No SPY weekly closes"):
        data_fetcher.get_benchmark_return(datetime.date(2026, 10, 23))
    assert fresh_caches == [2, 4]  # errors reach the retry decorator


def test_benchmark_fetch_retries_exceptions(monkeypatch):
    frame = to_yf_frame(pd.DataFrame({"SPY": [100.0, 112.0]}))
    outcomes = [ConnectionError("down"), frame]

    def download(*args, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("yfinance.download", download)
    assert data_fetcher.get_benchmark_return(datetime.date(2026, 10, 23)) == pytest.approx(0.12)


# ---------------------------------------------------------------------------
# Selection boundary (applies to every provider, including injected ones)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", INVALID_CAPS)
def test_select_universe_warns_and_ranks_invalid_caps_last(raw, caplog):
    assert engine.select_universe(
        ["BBB", "AAA"], {"AAA": 1e9, "BBB": raw}, 10
    ) == ["AAA", "BBB", "SPY"]
    assert "assigned zero and ranked last: BBB" in caplog.text


def test_select_universe_all_zero_keeps_constituent_order_but_rejects_empty_inputs():
    assert engine.select_universe(
        ["CCC", "AAA", "BBB"], {}, 2
    ) == ["CCC", "AAA", "SPY"]
    with pytest.raises(MarketCapError, match="No screened"):
        engine.select_universe([], {}, 2)


def test_select_universe_ranks_numeric_strings_and_numpy_caps():
    caps = {"AAA": np.int64(3), "BBB": "7", "CCC": np.float32(5)}
    assert engine.select_universe(["AAA", "BBB", "CCC"], caps, 2) == ["BBB", "CCC", "SPY"]


class InjectedProvider:
    def __init__(self, caps=None, closes=None, cap_error=None):
        self.tickers = TICKERS
        self.caps = caps if caps is not None else {t: (30 - i) * 1e9 for i, t in enumerate(TICKERS)}
        self.closes = closes if closes is not None else make_weekly_closes(TICKERS, end="2026-10-23")
        self.cap_error = cap_error
        self.calls = []

    def get_constituents(self, as_of_date):
        self.calls.append(("constituents", as_of_date))
        return fake_sp500_df(self.tickers)

    def get_market_caps(self, tickers, as_of_date):
        self.calls.append(("caps", as_of_date))
        if self.cap_error is not None:
            raise self.cap_error
        return self.caps

    def get_weekly_closes(self, tickers, start, end):
        self.calls.append(("prices", start, end))
        return self.closes[[t for t in tickers if t in self.closes.columns]]


@pytest.mark.parametrize("mod_key", ["app", "cb_taxable_portfolio_app"])
def test_all_zero_injected_caps_produce_a_portfolio_with_traced_warning(app_modules, monkeypatch, mod_key):
    monkeypatch.setattr("protocol_engine.FactorAnalyzer", FakeFactorAnalyzer)
    provider = InjectedProvider(caps=dict.fromkeys(TICKERS, 0))
    trace = SelectionTrace()
    result = app_modules[mod_key].run_pipeline(datetime.date(2026, 10, 23), BENCHMARK_GATE,
                                              provider=provider, trace=trace)
    assert trace.status == "selected"
    assert trace.market_cap_universe == TICKERS and not result[3].empty
    assert "assigned zero and ranked last" in trace.selection_notes[0]
    assert [call for call in provider.calls if call[0] == "prices"]


def test_provider_exception_is_wrapped_and_traced(app_modules):
    trace = SelectionTrace()
    provider = InjectedProvider(cap_error=ConnectionError("https://x?apikey=SECRET"))
    with pytest.raises(MarketDataError, match="Market-cap download failed") as raised:
        app_modules["app"].run_pipeline(datetime.date(2026, 10, 23), None,
                                        provider=provider, trace=trace)
    assert "SECRET" not in str(raised.value)
    assert isinstance(raised.value.__cause__, ConnectionError)
    assert trace.status == "market_data_failed"


def test_missing_spy_fails_benchmark_instead_of_zero(app_modules, monkeypatch):
    monkeypatch.setattr("protocol_engine.FactorAnalyzer", FakeFactorAnalyzer)
    closes = make_weekly_closes(TICKERS, end="2026-10-23").drop(columns="SPY")
    trace = SelectionTrace()
    for gate in (BENCHMARK_GATE, None, 0.05):
        with pytest.raises(BenchmarkError, match="SPY weekly closes are missing"):
            app_modules["app"].run_pipeline(datetime.date(2026, 10, 23), gate,
                                            provider=InjectedProvider(closes=closes), trace=trace)
        assert trace.status == "benchmark_failed"


@pytest.mark.parametrize("gate,error", [(float("nan"), BenchmarkError), (math.inf, BenchmarkError),
                                        ("spy", ValueError)])
def test_invalid_gate_threshold_fails_clearly(app_modules, gate, error):
    provider = InjectedProvider()
    trace = SelectionTrace()
    with pytest.raises(error):
        app_modules["app"].run_pipeline(datetime.date(2026, 10, 23), gate, provider=provider,
                                        trace=trace)
    assert provider.calls == []
    expected = "benchmark_failed" if error is BenchmarkError else "selection_failed"
    assert (trace.status, len(trace.selection_notes)) == (expected, 1)


def test_backtest_records_market_cap_warning_and_continues(app_modules, monkeypatch):
    bt = app_modules["backtest_engine"]
    monkeypatch.setattr("protocol_engine.FactorAnalyzer", FakeFactorAnalyzer)
    provider = InjectedProvider(caps={})
    provider.get_benchmark_return = lambda date: 0.1
    traces = []
    assert bt.compute_backtest_year(2026, provider=provider, traces=traces) is not None
    assert traces[0].status == "selected" and traces[0].backtest_status == "completed"
    assert len(traces[0].selection_notes) == 1
    assert "assigned zero and ranked last" in traces[0].selection_notes[0]


# ---------------------------------------------------------------------------
# Performance gate: default (same window), custom and off; selected-date flow
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod_key", ["app", "cb_taxable_portfolio_app"])
@pytest.mark.parametrize("as_of", [datetime.date(2025, 12, 31), datetime.date(2026, 10, 23)])
def test_default_gate_uses_pipeline_spy_window_and_selected_date(
        app_modules, monkeypatch, mod_key, as_of):
    mod = app_modules[mod_key]
    _, captured, closes = install_characterization_inputs(monkeypatch, mod, "normal")
    trace = SelectionTrace()
    result = mod.run_pipeline(as_of, BENCHMARK_GATE, trace=trace)

    spy = closes["SPY"].iloc[-1] / closes["SPY"].iloc[-53] - 1.0
    assert result[0] == pytest.approx(spy, abs=0)
    returns = closes.iloc[-1] / closes.iloc[-53] - 1.0
    assert set(result[4].index) == set(returns.drop("SPY")[returns.drop("SPY") >= spy].index)
    # Identical to an explicit threshold equal to the in-run SPY return.
    assert output_fingerprint(result) == output_fingerprint(mod.run_pipeline(as_of, result[0]))
    assert (trace.gate_mode, trace.gate_threshold) == ("benchmark", float(result[0]))
    assert trace.benchmark_window_start == closes.index[-53].date().isoformat()
    assert trace.benchmark_window_end == closes.index[-1].date().isoformat()
    assert trace.rebalance_date == as_of.isoformat()
    assert captured["constituents_date"] == captured["market_caps_date"] == as_of
    assert captured["download"][1]["end"] == as_of
    assert captured["download"][1]["start"] == as_of - datetime.timedelta(days=742)


@pytest.mark.parametrize("mod_key", ["app", "cb_taxable_portfolio_app"])
def test_custom_threshold_and_gate_off_are_respected(app_modules, monkeypatch, mod_key):
    mod = app_modules[mod_key]
    _, _, closes = install_characterization_inputs(monkeypatch, mod, "normal")
    returns = (closes.iloc[-1] / closes.iloc[-53] - 1.0).drop("SPY")
    custom = float(returns.sort_values().iloc[5])
    trace = SelectionTrace()
    survivors = mod.run_pipeline(datetime.date(2026, 10, 23), custom, trace=trace)[4]
    assert set(survivors.index) == set(returns[returns >= custom].index)
    assert (trace.gate_mode, trace.gate_threshold) == ("fixed", custom)

    survivors = mod.run_pipeline(datetime.date(2026, 10, 23), None, trace=trace)[4]
    assert set(survivors.index) == set(returns.index)
    assert (trace.gate_mode, trace.gate_threshold) == ("off", None)


# ---------------------------------------------------------------------------
# Dashboards: graceful errors, refresh, displayed gate (Streamlit AppTest)
# ---------------------------------------------------------------------------

def button(page, label):
    return next(item for item in page.button if item.label == label)


def open_dashboard(entry, monkeypatch, provider):
    import data_provider

    for name in ("get_constituents", "get_market_caps", "get_weekly_closes"):
        monkeypatch.setattr(data_provider.DEFAULT_PROVIDER, name, getattr(provider, name))
    monkeypatch.setattr(data_provider.DEFAULT_PROVIDER, "get_benchmark_return",
                        lambda date: pytest.fail("sidebar must not prefetch a separate benchmark"))
    monkeypatch.setattr("protocol_engine.FactorAnalyzer", FakeFactorAnalyzer)
    page = AppTest.from_file(str(ROOT / entry)).run(timeout=30)
    button(page, "Open App").click().run(timeout=30)
    assert not page.exception
    return page


@pytest.mark.parametrize("entry", ["app.py", "cb_taxable_portfolio_app.py"])
def test_dashboard_reports_zero_cap_warning_and_still_builds_portfolio(monkeypatch, entry):
    page = open_dashboard(entry, monkeypatch, InjectedProvider(caps=dict.fromkeys(TICKERS, 0.0)))
    button(page, "Run Protocol").click().run(timeout=30)
    assert not page.exception
    assert not page.error
    assert any("assigned zero and ranked last" in e.value for e in page.warning)
    assert page.metric and page.dataframe


@pytest.mark.parametrize("entry", ["app.py", "cb_taxable_portfolio_app.py"])
def test_dashboard_default_gate_shows_applied_threshold_window_and_date(monkeypatch, entry):
    provider = InjectedProvider()
    page = open_dashboard(entry, monkeypatch, provider)
    page.sidebar.date_input[0].set_value(datetime.date(2026, 10, 23)).run(timeout=30)
    button(page, "Run Protocol").click().run(timeout=30)
    assert not page.exception and not page.error
    closes = provider.closes
    spy = closes["SPY"].iloc[-1] / closes["SPY"].iloc[-53] - 1.0
    info = " ".join(item.value for item in page.info)
    assert "As of 2026-10-23" in info
    assert f"T12M return ≥ {spy:.2%}" in info
    assert f"{closes.index[-53].date()} to {closes.index[-1].date()}" in info
    assert "10 screened candidates ranked" in info
    assert ("caps", datetime.date(2026, 10, 23)) in provider.calls
    assert ("prices", datetime.date(2026, 10, 23) - datetime.timedelta(days=742),
            datetime.date(2026, 10, 23)) in provider.calls


@pytest.mark.parametrize("entry", ["app.py", "cb_taxable_portfolio_app.py"])
def test_dashboard_custom_and_off_modes(monkeypatch, entry):
    page = open_dashboard(entry, monkeypatch, InjectedProvider())
    page.sidebar.radio[0].set_value("Custom threshold").run(timeout=30)
    custom = next(item for item in page.sidebar.number_input
                  if item.label.startswith("Custom Minimum"))
    custom.set_value(12.5).run(timeout=30)
    button(page, "Run Protocol").click().run(timeout=30)
    assert "T12M return ≥ 12.50% (custom threshold)" in " ".join(i.value for i in page.info)

    page.sidebar.toggle[0].set_value(False).run(timeout=30)
    button(page, "Run Protocol").click().run(timeout=30)
    assert not page.exception
    assert any(i.value.startswith("**As of") and "performance gate: off" in i.value
               for i in page.info)


def test_dashboard_refresh_clears_market_data_caches(monkeypatch):
    cleared = []
    monkeypatch.setattr(data_fetcher, "clear_market_data_caches", lambda: cleared.append(True))
    page = open_dashboard("app.py", monkeypatch, InjectedProvider())
    button(page, "Refresh market data").click().run(timeout=30)
    assert cleared == [True]
    assert any("caches cleared" in item.value for item in page.sidebar.success)
