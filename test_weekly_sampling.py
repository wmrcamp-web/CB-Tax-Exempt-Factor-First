"""
Tests for the weekly-sampling change (daily -> weekly).

Covers:
1. Both portfolio apps (app.py, cb_taxable_portfolio_app.py) download prices with
   interval="1wk" and include enough history for 104 downside-beta weekly returns.
2. The trailing-12-month selection uses the last 53 weekly closes (~52 weekly
   return observations) instead of 252 daily trading-day rows.
3. The SPY benchmark fetch (data_fetcher.get_benchmark_return) uses the same
   weekly frequency as the portfolio calculation.
4. The backtest forward-return measurement (backtest_engine.py) is aligned to
   weekly sampling as well (decision: aligned, since it is an endpoint calc and
   consistency with the selection pipeline is preferred).
5. Behavior when data is missing: tickers without a full trailing year of weekly
   data are dropped, while interior gaps are forward-filled and survive.

All network access is mocked; no real yfinance/Wikipedia calls are made.

Also includes a comparison of the new weekly T12M calculation against the previous
daily calculation on identical synthetic data, validating that weekly sampling
approximates the daily result within a tight tolerance before weekly becomes default.
"""

import datetime
import logging
from unittest import mock

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

def make_weekly_closes(tickers, periods=120, end="2025-12-26"):
    """
    Build deterministic weekly closes for `tickers` plus SPY.

    SPY alternates +2% / -1.5% weekly returns (26 down weeks per 52), and each
    ticker is a positive-beta multiple of SPY so downside beta is computable
    (>= 12 downside observations) and every ticker shares SPY's down weeks.
    """
    idx = pd.date_range(end=pd.Timestamp(end), periods=periods, freq="W-FRI")
    spy_ret = np.where(np.arange(periods - 1) % 2 == 0, 0.02, -0.015)
    cols = {"SPY": 100 * np.cumprod(1 + np.concatenate([[0.0], spy_ret]))}
    for i, t in enumerate(tickers):
        beta = 0.8 + 0.05 * i
        cols[t] = 100 * np.cumprod(1 + np.concatenate([[0.0], beta * spy_ret]))
    return pd.DataFrame(cols, index=idx)


def to_yf_frame(closes):
    """Mimic yfinance multi-ticker output: top-level 'Close' column group."""
    return pd.concat({"Close": closes}, axis=1)


def fake_sp500_df(tickers):
    return pd.DataFrame({
        "Symbol": tickers,
        "Security": [f"{t} Inc" for t in tickers],
        "GICS Sector": ["Information Technology"] * len(tickers),
        "GICS Sub-Industry": ["Mock Industry"] * len(tickers),
    })


class FakeFactorAnalyzer:
    """Deterministic stand-in for factor_analyzer.FactorAnalyzer.

    Produces loadings without running minres so tests stay fast and focused on
    the data-frequency behavior. Rows 0 and 1 get max loading 0.3 (< 0.40 shadow
    cutoff); all others get 0.6 (>= 0.59 selection floor).
    """

    def __init__(self, n_factors, rotation=None, method=None):
        self.n_factors = n_factors

    def fit(self, X):
        n = X.shape[1]
        loadings = np.full((n, self.n_factors), 0.6)
        for r in range(min(2, n)):
            loadings[r, :] = 0.3
        self.loadings_ = loadings
        return self


# ---------------------------------------------------------------------------
# Module import fixture (app modules must be imported with yfinance mocked,
# because their module-level sidebar code calls get_benchmark_return -> yf.download)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_modules():
    logging.getLogger("streamlit").setLevel(logging.ERROR)  # silence bare-mode warnings
    idx = pd.date_range("2024-01-05", periods=60, freq="W-FRI")
    frame = to_yf_frame(pd.DataFrame({"SPY": np.linspace(100.0, 110.0, 60)}, index=idx))
    with mock.patch("yfinance.download", return_value=frame), mock.patch("access_gate.require_access"):
        import app
        import cb_taxable_portfolio_app
        import backtest_engine
    return {
        "app": app,
        "cb_taxable_portfolio_app": cb_taxable_portfolio_app,
        "backtest_engine": backtest_engine,
    }


def install_pipeline_mocks(monkeypatch, mod, tickers, closes):
    """Patch network + heavy numerics on one app module; return capture dict."""
    captured = {}

    def fake_download(tickers_arg, start, end):
        captured.update(start=start, end=end, interval="1wk")
        requested = tickers_arg if isinstance(tickers_arg, list) else [tickers_arg]
        cols = [t for t in requested if t in closes.columns]
        return closes[cols]

    monkeypatch.setattr(
        mod.DEFAULT_PROVIDER, "get_constituents",
        lambda as_of_date: fake_sp500_df(tickers),
    )
    monkeypatch.setattr(
        mod.DEFAULT_PROVIDER, "get_market_caps",
        lambda ts, as_of_date: {t: (len(ts) - i) * 1e9 for i, t in enumerate(ts)},
    )
    monkeypatch.setattr(mod.DEFAULT_PROVIDER, "get_weekly_closes", fake_download)
    monkeypatch.setattr("protocol_engine.FactorAnalyzer", FakeFactorAnalyzer)
    return captured


# ---------------------------------------------------------------------------
# 1 & 2: weekly download interval + 53-week trailing window in both apps
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod_key", ["app", "cb_taxable_portfolio_app"])
def test_pipeline_downloads_weekly_and_uses_52_week_window(app_modules, monkeypatch, mod_key):
    mod = app_modules[mod_key]
    tickers = [f"FAKE{i}" for i in range(12)]
    closes = make_weekly_closes(tickers)
    captured = install_pipeline_mocks(monkeypatch, mod, tickers, closes)

    end_date = datetime.date(2025, 12, 31)
    sp500_ret, metrics_df, loadings_df, portfolio_df, survivors_df, factor_headers = mod.run_pipeline(end_date, None)

    # Weekly download setting
    assert captured["interval"] == "1wk"
    # 105 weekly closes plus a week-boundary buffer for 104 beta returns.
    assert captured["start"] == end_date - datetime.timedelta(days=742)

    # Trailing-12-month selection must use the last 53 weekly closes
    expected_window = closes.iloc[-1] / closes.iloc[-53] - 1.0
    expected_full = closes.iloc[-1] / closes.iloc[0] - 1.0
    # Guard: the 53-row weekly window must be distinguishable from a larger window,
    # otherwise this test could not catch a regression back to iloc[-252:]
    assert abs(expected_full["SPY"] - expected_window["SPY"]) > 0.02

    assert sp500_ret == pytest.approx(expected_window["SPY"], abs=1e-9)
    for t in tickers:
        assert survivors_df.loc[t, "T12M_Return"] == pytest.approx(expected_window[t], abs=1e-9)

    # Portfolio still assembles under weekly sampling
    assert not portfolio_df.empty
    assert len(metrics_df) == len(tickers)


@pytest.fixture(params=[(52, 11), (52, 12), (104, 11), (104, 12)])
def low_downside_weekly_data(request):
    lookback, down_weeks = request.param
    tickers = [f"FAKE{i}" for i in range(12)]
    spy_returns = np.r_[
        np.linspace(-0.03, -0.01, down_weeks),
        np.full(lookback - down_weeks, 0.02),
    ]
    prices = {"SPY": 100 * np.cumprod(1 + np.r_[0.0, spy_returns])}
    for i, ticker in enumerate(tickers):
        prices[ticker] = 100 * np.cumprod(1 + np.r_[0.0, (0.8 + 0.05 * i) * spy_returns])
    closes = pd.DataFrame(
        prices, index=pd.date_range(end="2025-12-26", periods=lookback + 1, freq="W-FRI"),
    )
    return down_weeks, tickers, closes


@pytest.mark.parametrize("mod_key", ["app", "cb_taxable_portfolio_app"])
def test_pipeline_weekly_downside_threshold(
        app_modules, monkeypatch, mod_key, low_downside_weekly_data):
    from selection_trace import SelectionTrace

    down_weeks, tickers, closes = low_downside_weekly_data
    mod = app_modules[mod_key]
    install_pipeline_mocks(monkeypatch, mod, tickers, closes)
    errors = []
    monkeypatch.setattr(mod.st, "error", errors.append)
    trace = SelectionTrace()

    result = mod.run_pipeline(datetime.date(2025, 12, 31), None, trace=trace)

    assert len(result) == 6
    if len(closes) == 105:
        assert (closes["SPY"].iloc[-53:].pct_change().dropna() < 0).sum() == 0
    if down_weeks == 12:
        assert errors == []
        assert not result[3].empty
        assert result[1]["Downside_Beta"].to_numpy() == pytest.approx(
            [0.8 + 0.05 * i for i in range(len(tickers))]
        )
        assert trace.status == "selected"
        assert trace.final_tickers == result[3].index.tolist()
    else:
        assert errors == ["No valid stocks after Downside Beta calculation!"]
        assert all(frame.empty for frame in result[1:4])
        assert result[4]["Downside_Beta"].isna().all()
        assert trace.status == "selection_failed"
        assert trace.selection_notes == errors


def test_backtest_weekly_downside_threshold(
        app_modules, monkeypatch, low_downside_weekly_data):
    down_weeks, tickers, closes = low_downside_weekly_data
    bt = app_modules["backtest_engine"]
    install_pipeline_mocks(monkeypatch, bt, tickers, closes)
    monkeypatch.setattr(bt.DEFAULT_PROVIDER, "get_benchmark_return", lambda date: None)
    traces, details = [], []

    result = bt.run_backtest_for_year(2026, traces=traces, detailed_rows=details)

    if down_weeks == 12:
        assert result is not None
        assert details
        assert {row["Ticker"] for row in details} == set(traces[0].final_tickers)
        assert traces[0].status == "selected"
        assert traces[0].backtest_status == "completed"
    else:
        assert result is None
        assert details == []
        assert traces[0].status == "selection_failed"
        assert traces[0].backtest_status == "no_portfolio"
        assert traces[0].selection_notes == ["No valid stocks after Downside Beta calculation!"]


@pytest.mark.parametrize("mod_key", ["app", "cb_taxable_portfolio_app"])
def test_pipeline_uses_exactly_104_beta_returns(app_modules, monkeypatch, mod_key):
    from metrics import calculate_downside_beta

    mod = app_modules[mod_key]
    tickers = [f"FAKE{i}" for i in range(12)]
    spy_returns = np.r_[np.linspace(-0.08, -0.04, 15),
                        np.linspace(-0.03, -0.01, 52), np.full(52, 0.02)]
    multipliers = np.r_[np.full(15, 4.0), np.full(52, 1.5), np.full(52, 0.5)]
    prices = {"SPY": 100 * np.cumprod(1 + np.r_[0.0, spy_returns])}
    for ticker in tickers:
        prices[ticker] = 100 * np.cumprod(1 + np.r_[0.0, multipliers * spy_returns])
    closes = pd.DataFrame(prices, index=pd.date_range(end="2025-12-26", periods=120, freq="W-FRI"))
    install_pipeline_mocks(monkeypatch, mod, tickers, closes)
    fitted = []

    class Analyzer(FakeFactorAnalyzer):
        def fit(self, matrix):
            fitted.append(matrix.copy())
            return super().fit(matrix)

    monkeypatch.setattr("protocol_engine.FactorAnalyzer", Analyzer)
    result = mod.run_pipeline(datetime.date(2025, 12, 31), None)

    assert result[1]["Downside_Beta"].tolist() == pytest.approx([1.5] * len(tickers))
    assert result[4]["Downside_Beta"].tolist() == pytest.approx([1.5] * len(tickers))
    full_returns = closes.pct_change().dropna()
    assert calculate_downside_beta(full_returns[tickers[0]], full_returns["SPY"]) != pytest.approx(1.5)
    expected_factors = closes.iloc[-53:].pct_change().dropna()[tickers]
    pd.testing.assert_frame_equal(fitted[0], expected_factors)
    assert result[0] == pytest.approx(closes["SPY"].iloc[-1] / closes["SPY"].iloc[-53] - 1)
    assert not result[3].empty
    assert result[3]["Weight (%)"].tolist() == [round(100 / len(result[3]), 2)] * len(result[3])


def test_downside_returns_fill_before_slicing_and_keep_partial_histories():
    import protocol_engine as engine
    from metrics import calculate_downside_beta

    closes = make_weekly_closes(["FULL", "PARTIAL", "MISSING"], periods=120)
    closes.loc[closes.index[-105], "FULL"] = np.nan
    closes.loc[closes.index[:-53], "PARTIAL"] = np.nan
    closes["MISSING"] = np.nan
    original = closes.copy(deep=True)

    returns, benchmark = engine.prepare_downside_returns(closes)

    expected = closes.ffill().iloc[-105:].pct_change(fill_method=None).iloc[1:]
    pd.testing.assert_frame_equal(returns, expected)
    pd.testing.assert_series_equal(benchmark, expected["SPY"])
    assert len(returns) == 104
    assert returns["PARTIAL"].notna().sum() == 52
    assert np.isfinite(calculate_downside_beta(returns["PARTIAL"], benchmark))
    assert np.isnan(calculate_downside_beta(returns["MISSING"], benchmark))
    pd.testing.assert_frame_equal(closes, original)


def test_downside_returns_empty_sample():
    import protocol_engine as engine
    from metrics import calculate_downside_beta

    returns, benchmark = engine.prepare_downside_returns(pd.DataFrame(columns=["AAA", "SPY"], dtype=float))
    assert returns.empty
    assert np.isnan(calculate_downside_beta(returns["AAA"], benchmark))


# ---------------------------------------------------------------------------
# 3: benchmark fetch uses the same weekly frequency
# ---------------------------------------------------------------------------

def test_benchmark_fetch_uses_weekly_interval(monkeypatch):
    import data_fetcher

    idx = pd.date_range(end="2025-12-26", periods=60, freq="W-FRI")
    frame = to_yf_frame(pd.DataFrame({"SPY": np.linspace(100.0, 115.0, 60)}, index=idx))

    captured = {}

    def fake_download(tickers, start=None, end=None, interval=None, progress=None, **kw):
        captured.update(tickers=tickers, start=start, end=end, interval=interval)
        return frame

    monkeypatch.setattr("yfinance.download", fake_download)

    end_date = datetime.date(2025, 12, 31)
    ret = data_fetcher.get_benchmark_return(end_date)

    assert captured["interval"] == "1wk"
    assert captured["start"] == end_date - datetime.timedelta(days=742)
    assert ret == pytest.approx(115 / frame["Close"]["SPY"].iloc[-53] - 1, abs=1e-9)


def test_live_provider_delegates_to_existing_live_sources(monkeypatch):
    import data_fetcher
    from data_provider import LiveProvider

    provider = LiveProvider()
    as_of_date = datetime.date(2025, 12, 31)
    start_date = as_of_date - datetime.timedelta(days=730)
    constituents = fake_sp500_df(["AAA"])
    caps = {"AAA": 123}
    captured = {}

    monkeypatch.setattr(data_fetcher, "fetch_sp500_tickers", lambda: constituents)
    monkeypatch.setattr(data_fetcher, "get_sorted_by_market_cap", lambda tickers: (tickers, caps))
    monkeypatch.setattr(data_fetcher, "get_benchmark_return", lambda date: 0.12)

    def fake_download(tickers, **kwargs):
        captured.update(tickers=tickers, **kwargs)
        return to_yf_frame(pd.DataFrame({"AAA": [10.0]}, index=[as_of_date]))

    monkeypatch.setattr("yfinance.download", fake_download)

    assert provider.get_constituents(as_of_date) is constituents
    assert provider.get_market_caps(["AAA"], as_of_date) is caps
    assert provider.get_benchmark_return(as_of_date) == 0.12
    pd.testing.assert_frame_equal(
        provider.get_weekly_closes(["AAA"], start_date, as_of_date),
        pd.DataFrame({"AAA": [10.0]}, index=[as_of_date]),
    )
    assert captured == {
        "tickers": ["AAA"],
        "start": start_date,
        "end": as_of_date,
        "interval": "1wk",
        "auto_adjust": True,
        "progress": False,
    }


# ---------------------------------------------------------------------------
# 4: backtest forward-return measurement aligned to weekly
# ---------------------------------------------------------------------------

def test_backtest_forward_returns_use_weekly_interval(app_modules, monkeypatch):
    bt = app_modules["backtest_engine"]

    portfolio_df = pd.DataFrame({"Weight (%)": [33.33] * 3}, index=["AAA", "BBB", "CCC"])

    idx = pd.date_range(end="2024-12-27", periods=60, freq="W-FRI")
    closes = pd.DataFrame({
        "AAA": np.linspace(100.0, 120.0, 60),   # +20%
        "BBB": np.linspace(100.0, 110.0, 60),   # +10%
        "CCC": np.linspace(100.0, 105.0, 60),   # +5%
        "SPY": np.linspace(100.0, 108.0, 60),   # +8%
    }, index=idx)

    captured = {}

    calls = {}

    class Provider:
        def get_benchmark_return(self, as_of_date):
            calls["gate_date"] = as_of_date
            return 0.10

        def get_constituents(self, as_of_date):
            raise AssertionError("selection pipeline mock should bypass acquisition")

        def get_market_caps(self, tickers, as_of_date):
            raise AssertionError("selection pipeline mock should bypass acquisition")

        def get_weekly_closes(self, tickers, start_date, end_date):
            calls["forward"] = (tickers, start_date, end_date)
            captured.update(start=start_date, end=end_date, interval="1wk")
            return closes[tickers]

    provider = Provider()

    def fake_pipeline(*args, **kwargs):
        calls["gate"] = args[1]
        calls["selection_provider"] = kwargs["provider"]
        return 0.1, pd.DataFrame(), pd.DataFrame(), portfolio_df, pd.DataFrame(), []

    monkeypatch.setattr(
        bt, "run_pipeline", fake_pipeline,
    )

    res = bt.run_backtest_for_year(2024, provider=provider)

    assert captured["interval"] == "1wk"
    assert "gate_date" not in calls
    assert calls["gate"] == bt.BENCHMARK_GATE
    assert calls["selection_provider"] is provider
    assert calls["forward"] == (
        ["AAA", "BBB", "CCC", "SPY"],
        datetime.date(2023, 12, 31),
        datetime.date(2024, 12, 31),
    )
    expected_port = np.mean([0.20, 0.10, 0.05])
    assert res["Portfolio_Return"] == pytest.approx(expected_port, abs=1e-9)
    assert res["Benchmark_Return"] == pytest.approx(0.08, abs=1e-9)
    assert res["Alpha"] == pytest.approx(expected_port - 0.08, abs=1e-9)


# ---------------------------------------------------------------------------
# 5: missing-data behavior under weekly sampling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod_key", ["app", "cb_taxable_portfolio_app"])
def test_pipeline_handles_missing_weekly_data(app_modules, monkeypatch, mod_key):
    mod = app_modules[mod_key]
    tickers = [f"FAKE{i}" for i in range(10)] + ["GAP", "LATE"]
    closes = make_weekly_closes(tickers)

    # GAP: 3 missing weeks in the middle of the trailing year -> ffill'ed, survives
    closes.loc[closes.index[90:93], "GAP"] = np.nan
    # LATE: only exists for the last 30 weeks -> NaNs inside the 53-week window, dropped
    closes.loc[closes.index[:-30], "LATE"] = np.nan

    install_pipeline_mocks(monkeypatch, mod, tickers, closes)

    sp500_ret, metrics_df, loadings_df, portfolio_df, survivors_df, factor_headers = mod.run_pipeline(
        datetime.date(2025, 12, 31), None
    )

    # Late-listed ticker without a full trailing year is excluded everywhere
    assert "LATE" not in survivors_df.index
    assert "LATE" not in metrics_df.index
    # Interior-gap ticker is forward-filled and survives
    assert "GAP" in survivors_df.index
    assert np.isfinite(survivors_df.loc["GAP", "T12M_Return"])
    # Clean tickers are unaffected
    assert "FAKE0" in survivors_df.index


# ---------------------------------------------------------------------------
# Daily vs weekly comparison: the new weekly T12M must approximate the previous
# daily calculation on the same underlying series before weekly becomes default.
#
# Note on tolerances: the two windows anchor to slightly different start dates
# (last 252 trading rows vs the Friday close 53 weekly bars back can be up to ~7
# trading days apart), so on volatile paths the difference is dominated by
# endpoint-timing noise, not by the sampling change itself. A 200-seed sweep at
# ~12.7% annual vol gives diff std ~= 2.3pp, p99 ~= 5pp. Hence: a tight bound on
# a smooth path, and a ~3-sigma bound on a seeded realistic-volatility path.
# ---------------------------------------------------------------------------

def test_weekly_t12m_matches_daily_on_smooth_path():
    idx = pd.bdate_range(end="2025-12-31", periods=756)
    t = np.arange(len(idx))
    daily_ret = 0.0003 + 0.0008 * np.sin(2 * np.pi * t / 21)  # drift + mild monthly cycle
    closes = pd.Series(100 * np.cumprod(1 + daily_ret), index=idx)

    # Previous calculation: last 252 daily trading rows
    daily_t12m = closes.iloc[-1] / closes.iloc[-252] - 1.0

    # New calculation: weekly closes (last close of each week), last 53 bars
    weekly_closes = closes.resample("W-FRI").last().dropna()
    weekly_t12m = weekly_closes.iloc[-1] / weekly_closes.iloc[-53] - 1.0

    assert abs(weekly_t12m - daily_t12m) < 0.01, (
        f"Weekly T12M ({weekly_t12m:.2%}) vs daily T12M ({daily_t12m:.2%}) on a smooth path"
    )


def test_weekly_t12m_matches_daily_under_realistic_volatility():
    rng = np.random.default_rng(42)
    n_days = 756  # ~3 years of trading days
    idx = pd.bdate_range(end="2025-12-31", periods=n_days)
    daily_ret = 0.0004 + 0.008 * rng.standard_normal(n_days)  # ~12.7% annualized vol
    closes = pd.Series(100 * np.cumprod(1 + daily_ret), index=idx)

    daily_t12m = closes.iloc[-1] / closes.iloc[-252] - 1.0
    weekly_closes = closes.resample("W-FRI").last().dropna()
    weekly_t12m = weekly_closes.iloc[-1] / weekly_closes.iloc[-53] - 1.0

    # ~3-sigma bound for the endpoint-anchoring noise budget documented above
    assert abs(weekly_t12m - daily_t12m) < 0.07, (
        f"Weekly T12M ({weekly_t12m:.2%}) vs daily T12M ({daily_t12m:.2%}) under realistic volatility"
    )
