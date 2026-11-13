"""Offline Sharadar contracts: no credentials or licensed data in fixtures."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

import sharadar_provider as sp
from data_errors import MarketCapError, MarketDataError
from sharadar_backtest import configured_provider


class Tables:
    def __init__(self, **tables):
        self.tables = tables
        self.calls = []

    def table(self, table_name, fields, **filters):
        table = table_name
        self.calls.append((table, filters))
        return self.tables[table].copy()


def roster():
    return pd.DataFrame([
        {"date": "2019-09-30", "action": "historical", "ticker": f"T{i}", "name": f"Company {i}"}
        for i in range(500)
    ])


def test_ticker_batches_respect_vendor_length_commas_and_preserve_order(tmp_path):
    provider = sp.SharadarProvider("fixture-only", tmp_path)
    provider._vendor_symbols = {"FIRST": "A" * 100, "SECOND": "B" * 99, "THIRD": "BRK.B"}
    batches = list(provider._batches(["FIRST", "SECOND", "THIRD", "FIRST", "SPY"]))
    assert batches == [
        (["FIRST", "SECOND"], "A" * 100 + "," + "B" * 99),
        (["THIRD", "SPY"], "BRK.B,SPY"),
    ]
    assert all(len(parameter) <= 200 for _, parameter in batches)
    assert list(provider._batches([])) == []


def test_ticker_batches_retain_company_limit_and_reject_oversized_identifier(tmp_path):
    provider = sp.SharadarProvider("fixture-only", tmp_path)
    provider._vendor_symbols = {str(i): str(i) for i in range(51)}
    batches = list(provider._batches(list(provider._vendor_symbols)))
    assert [len(batch) for batch, _ in batches] == [50, 1]
    provider._vendor_symbols["LONG"] = "X" * 201
    with pytest.raises(MarketDataError, match="parameter limit"):
        list(provider._batches(["LONG"]))


def labels(tmp_path, symbols, stamp="2019-12-31"):
    pd.DataFrame({
        "Symbol": symbols, "Security": symbols,
        "GICS Sector": "Industrials", "GICS Sub-Industry": "Synthetic industry",
    }).to_csv(tmp_path / f"sp500_{stamp}.csv", index=False)


def price_provider(tmp_path, stocks, funds=None):
    if funds is None:
        funds = pd.DataFrame([
            {"ticker": "SPY", "date": day, "closeadj": value}
            for day, value in [("2019-12-30", 100), ("2020-01-02", 101), ("2020-12-31", 110)]
        ])
    client = Tables(stocks=pd.DataFrame(stocks), funds=funds)
    provider = sp.SharadarProvider("fixture-only", tmp_path, client=client)
    provider._vendor_symbols = {"T0": "T0", "BRK-B": "BRK.B"}
    return provider


def test_membership_replays_effective_changes_without_current_universe(tmp_path):
    changes = pd.DataFrame([
        {"date": "2019-10-01", "action": "removed", "ticker": "T0", "name": "Removed"},
        {"date": "2019-10-01", "action": "added", "ticker": "BRK.B", "name": "Added"},
    ])
    client = Tables(sp500=pd.concat([roster(), changes], ignore_index=True))
    symbols = [f"T{i}" for i in range(1, 500)] + ["BRK-B"]
    labels(tmp_path, symbols)
    provider = sp.SharadarProvider("fixture-only", tmp_path, client=client)
    result = provider.get_constituents(date(2019, 12, 31))
    assert set(result["Symbol"]) == set(symbols)
    assert provider._vendor_symbols["BRK-B"] == "BRK.B"
    assert client.calls[0][1] == {"from": "1998-01-01", "to": "2019-12-31"}


@pytest.mark.parametrize("problem", ["future", "no-snapshot", "small", "unknown-action", "duplicate"])
def test_invalid_membership_is_rejected(tmp_path, problem):
    history = roster()
    if problem == "future":
        history.loc[0, "date"] = "2020-01-01"
    elif problem == "no-snapshot":
        history["action"] = "current"
    elif problem == "small":
        history = history.iloc[:20]
    elif problem == "unknown-action":
        history.loc[0, "action"] = "unrecognized"
    else:
        history = pd.concat([history, history.iloc[:1]], ignore_index=True)
    provider = sp.SharadarProvider("fixture-only", tmp_path, client=Tables(sp500=history))
    with pytest.raises(MarketDataError):
        provider.get_constituents(date(2019, 12, 31))


def test_gics_must_exist_and_match_historical_roster(tmp_path):
    provider = sp.SharadarProvider("fixture-only", tmp_path, client=Tables(sp500=roster()))
    with pytest.raises(MarketDataError, match="dated GICS"):
        provider.get_constituents(date(2019, 12, 31))
    labels(tmp_path, [f"T{i}" for i in range(499)] + ["NOT_A_MEMBER"])
    with pytest.raises(MarketDataError, match="exact historical roster"):
        provider.get_constituents(date(2019, 12, 31))


def test_caps_use_latest_dated_observation_and_convert_millions(tmp_path):
    client = Tables(daily=pd.DataFrame([
        {"ticker": "BRK.B", "date": "2019-12-27", "marketcap": "90"},
        {"ticker": "BRK.B", "date": "2019-12-30", "marketcap": "100"},
    ]))
    provider = sp.SharadarProvider("fixture-only", tmp_path, client=client)
    provider._vendor_symbols = {"BRK-B": "BRK.B"}
    assert provider.get_market_caps(["BRK-B"], date(2019, 12, 31)) == {"BRK-B": 100000000}
    assert client.calls[0][1]["ticker"] == "BRK.B"
    assert client.calls[0][1]["to"] == "2019-12-31"


@pytest.mark.parametrize("cap", ["0", "-1", "NaN", "infinity"])
def test_invalid_caps_never_fall_back_to_current_caps(tmp_path, cap):
    provider = sp.SharadarProvider("fixture-only", tmp_path, client=Tables(
        daily=pd.DataFrame([{"ticker": "T0", "date": "2019-12-30", "marketcap": cap}]),
    ))
    provider._vendor_symbols = {"T0": "T0"}
    with pytest.raises(MarketCapError):
        provider.get_market_caps(["T0"], date(2019, 12, 31))


def test_missing_or_future_market_caps_are_rejected(tmp_path):
    provider = sp.SharadarProvider("fixture-only", tmp_path, client=Tables(
        daily=pd.DataFrame([{"ticker": "T0", "date": "2020-01-02", "marketcap": "100"}]),
    ))
    provider._vendor_symbols = {"T0": "T0", "T1": "T1"}
    with pytest.raises(MarketCapError, match="unexpected"):
        provider.get_market_caps(["T0"], date(2019, 12, 31))
    provider.client.tables["daily"].loc[0, "date"] = "2019-12-30"
    with pytest.raises(MarketCapError, match="T1"):
        provider.get_market_caps(["T0", "T1"], date(2019, 12, 31))


def test_forward_prices_use_previous_close_and_do_not_backfill_from_future(tmp_path):
    provider = price_provider(tmp_path, [
        {"ticker": "T0", "date": day, "closeadj": value}
        for day, value in [("2019-12-30", 50), ("2020-01-02", 60), ("2020-12-31", 75)]
    ])
    prices = provider.get_forward_closes(["T0", "SPY"], date(2019, 12, 31), date(2020, 12, 31))
    assert prices.index.tolist() == [pd.Timestamp("2019-12-30"), pd.Timestamp("2020-12-31")]
    assert (prices.iloc[-1] / prices.iloc[0] - 1)["T0"] == pytest.approx(0.5)


@pytest.mark.parametrize("days", [
    [("2020-01-02", 60), ("2020-12-31", 75)],
    [("2019-12-30", 50), ("2020-01-02", 60)],
])
def test_unpriced_start_or_delisting_blocks_forward_return(tmp_path, days):
    provider = price_provider(tmp_path, [
        {"ticker": "T0", "date": day, "closeadj": value} for day, value in days
    ])
    with pytest.raises(MarketDataError):
        provider.get_forward_closes(["T0", "SPY"], date(2019, 12, 31), date(2020, 12, 31))


def test_weekly_prices_include_partial_week_without_future_prices(tmp_path):
    funds = pd.DataFrame([
        {"ticker": "SPY", "date": day, "closeadj": value}
        for day, value in [("2020-12-28", 100), ("2020-12-30", 102)]
    ])
    provider = price_provider(tmp_path, [
        {"ticker": "T0", "date": day, "closeadj": value}
        for day, value in [("2020-12-28", 50), ("2020-12-30", 52)]
    ], funds=funds)
    prices = provider.get_weekly_closes(["T0", "SPY"], date(2020, 12, 28), date(2020, 12, 30))
    assert prices.index.tolist() == [pd.Timestamp("2020-12-28")]
    assert prices.iloc[0].to_dict() == {"T0": 52, "SPY": 102}
    assert all(call[1]["to"] == "2020-12-30" for call in provider.client.calls)


def test_csv_client_paginates_and_retains_filters(monkeypatch):
    monkeypatch.setattr(sp, "PAGE_SIZE", 2)
    calls = []

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            text = ("ticker,date\nT0,2020-01-01\nT1,2020-01-01\n"
                    if kwargs["params"]["skip"] == 0 else "ticker,date\nT2,2020-01-01\n")
            return SimpleNamespace(status_code=200, text=text)

    frame = sp.SharadarClient("fixture-only", Session()).table(
        "stocks", ["ticker", "date"], **{"from": "2020-01-01", "to": "2020-01-02"},
    )
    assert frame["ticker"].tolist() == ["T0", "T1", "T2"]
    assert [call[1]["params"]["skip"] for call in calls] == [0, 2]
    assert all(call[1]["params"]["from"] == "2020-01-01" for call in calls)
    assert all(call[1]["allow_redirects"] is False for call in calls)


@pytest.mark.parametrize("outcome", ["network", "http", "schema"])
def test_client_errors_never_echo_keys_or_response_bodies(outcome):
    secret = "synthetic-secret-for-redaction"

    class Session:
        def get(self, url, **kwargs):
            if outcome == "network":
                raise requests.ConnectionError(f"https://example.invalid/?api_key={secret}")
            return SimpleNamespace(
                status_code=403 if outcome == "http" else 200,
                text=f"provider error containing {secret}",
            )

    with pytest.raises(MarketDataError) as caught:
        sp.SharadarClient(secret, Session()).table("stocks", ["ticker", "date"])
    assert secret not in str(caught.value)
    if outcome == "network":
        assert caught.value.__suppress_context__


def test_private_configuration_requires_key_and_prefers_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("SHARADAR_API_KEY", raising=False)
    with pytest.raises(MarketDataError, match="SHARADAR_API_KEY"):
        configured_provider()
    monkeypatch.setenv("SHARADAR_API_KEY", "fixture-only")
    monkeypatch.setenv("SHARADAR_GICS_DIR", str(tmp_path))
    provider = configured_provider({"SHARADAR_API_KEY": "other-fixture"})
    assert provider.client._api_key == "fixture-only"
    assert provider.gics_dir == tmp_path


def test_backtest_uses_strict_forward_provider_without_legacy_filling(monkeypatch, tmp_path):
    import backtest_engine as bt

    provider = price_provider(tmp_path, [
        {"ticker": "T0", "date": day, "closeadj": value}
        for day, value in [("2019-12-30", 50), ("2020-12-31", 75)]
    ])
    portfolio = pd.DataFrame(index=["T0"])
    portfolio["Weight (%)"] = 100.0
    monkeypatch.setattr(bt, "run_pipeline", lambda *args, **kwargs: (None, None, None, portfolio))
    result = bt.compute_backtest_year(2020, provider=provider)
    assert result.summary["Portfolio_Return"] == pytest.approx(0.5)
    assert result.summary["Benchmark_Return"] == pytest.approx(0.1)
    assert result.details[0]["Start_Price"] == 50
    assert result.details[0]["End_Price"] == 75


def test_results_source_is_opt_in_and_does_not_display_baseline_as_sharadar(monkeypatch, tmp_path):
    import backtest_exports as exports
    import backtest_results_page as page_module
    import sharadar_backtest
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(page_module, "require_access", lambda: None)
    monkeypatch.setattr(exports, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sharadar_backtest, "DATA_DIR", tmp_path / "isolated")
    monkeypatch.setenv("SHARADAR_API_KEY", "fixture-only")
    pd.DataFrame([{
        "Year": 2020, "Portfolio_Return": 0.99, "Benchmark_Return": 0.1, "Alpha": 0.89,
    }]).to_csv(tmp_path / "backtest_results.csv", index=False)
    page_path = Path(exports.__file__).parent / "pages" / "1_Backtest_Results.py"
    page = AppTest.from_file(str(page_path)).run(timeout=15)
    assert not page.exception
    assert len(page.get("download_button")) == 1
    page.radio[0].set_value("Sharadar historical results").run(timeout=15)
    assert not page.exception
    assert not page.get("download_button")
    assert any("No results have been generated" in message.value for message in page.info)


def test_cli_exits_unsuccessfully_when_years_are_unpublished(monkeypatch, tmp_path, capsys):
    import backtest_engine as bt
    import sharadar_backtest

    monkeypatch.setenv("SHARADAR_API_KEY", "fixture-only")
    monkeypatch.setattr(sharadar_backtest, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bt, "update_backtest_history", lambda **kwargs: None)
    monkeypatch.setattr("sys.argv", ["sharadar_backtest.py", "--start-year", "2020", "--end-year", "2020"])
    with pytest.raises(SystemExit) as caught:
        sharadar_backtest.main()
    assert caught.value.code == 1
    assert "No Sharadar years were published" in capsys.readouterr().err


def metadata(symbols):
    return pd.DataFrame([
        {"ticker": symbol, "name": symbol, "sector": "Industrials", "industry": "Machinery",
         "isdelisted": "N", "lastpricedate": "2026-11-12", "permaticker": str(index + 1)}
        for index, symbol in enumerate(symbols)
    ])


def test_current_sharadar_classifications_do_not_require_gics_files(tmp_path):
    client = Tables(sp500=roster(), tickers=metadata([f"T{i}" for i in range(500)]))
    # The fixture client returns a full roster; emulate the vendor ticker filter.
    original = client.table

    def filtered(resource, fields, **filters):
        frame = original(resource, fields, **filters)
        if resource == "tickers":
            return frame[frame["ticker"].isin(filters["ticker"].split(","))]
        return frame

    client.table = filtered
    provider = sp.SharadarProvider("fixture-only", tmp_path, client=client, sector_source="sharadar")
    result = provider.get_constituents(date(2019, 12, 31))
    assert len(result) == 500
    assert set(result["GICS Sector"]) == {"Industrials"}
    assert result["Security_ID"].nunique() == 500
    assert "not point-in-time GICS" in provider.notes[0]
    assert all("from" not in filters and filters["table"] == "fundamentals"
               for resource, filters in client.calls if resource == "tickers")


@pytest.mark.parametrize("confirmed", [True, False])
def test_delisted_exit_estimate_requires_confirmed_terminal_price(tmp_path, confirmed):
    provider = price_provider(tmp_path, [
        {"ticker": "T0", "date": day, "closeadj": value}
        for day, value in [("2019-12-30", 50), ("2020-01-02", 20)]
    ])
    provider.delisting_policy = "last_close_cash"
    master = metadata(["T0"])
    master.loc[0, "isdelisted"] = "Y" if confirmed else "N"
    master.loc[0, "lastpricedate"] = "2020-01-02"
    provider.client.tables["tickers"] = master
    if not confirmed:
        with pytest.raises(MarketDataError, match="not a confirmed"):
            provider.get_forward_closes(["T0", "SPY"], date(2019, 12, 31), date(2020, 12, 31))
        return
    prices = provider.get_forward_closes(["T0", "SPY"], date(2019, 12, 31), date(2020, 12, 31))
    assert prices.iloc[-1]["T0"] == 20
    assert "ESTIMATE" in provider.forward_notes["T0"]
    assert "0%" in provider.forward_notes["T0"]


def test_later_metadata_price_date_does_not_justify_filling_a_quote_gap(tmp_path):
    provider = price_provider(tmp_path, [
        {"ticker": "T0", "date": day, "closeadj": value}
        for day, value in [("2019-12-30", 50), ("2020-01-02", 20)]
    ])
    provider.delisting_policy = "last_close_cash"
    master = metadata(["T0"])
    master.loc[0, "isdelisted"] = "Y"
    master.loc[0, "lastpricedate"] = "2021-01-02"
    provider.client.tables["tickers"] = master
    with pytest.raises(MarketDataError, match="not a confirmed"):
        provider.get_forward_closes(["T0", "SPY"], date(2019, 12, 31), date(2020, 12, 31))
    assert not provider.forward_notes


def test_estimated_return_disclosure_survives_summary_and_detail_export(monkeypatch, tmp_path):
    import backtest_engine as bt
    import backtest_exports as exports

    provider = price_provider(tmp_path, [
        {"ticker": "T0", "date": day, "closeadj": value}
        for day, value in [("2019-12-30", 50), ("2020-01-02", 20)]
    ])
    provider.delisting_policy = "last_close_cash"
    provider.sector_source = "sharadar"
    master = metadata(["T0"])
    master.loc[0, ["isdelisted", "lastpricedate"]] = ["Y", "2020-01-02"]
    provider.client.tables["tickers"] = master
    portfolio = pd.DataFrame({"Weight (%)": [100.0]}, index=["T0"])
    monkeypatch.setattr(bt, "run_pipeline", lambda *args, **kwargs: (None, None, None, portfolio))
    summary, details, failures = exports.rebuild_years([2020], provider=provider)
    assert not failures
    assert summary.iloc[0]["Estimated_Exit_Count"] == 1
    assert summary.iloc[0]["Portfolio_Return"] == pytest.approx(-0.6)
    assert details.iloc[0]["Exit_Valuation"].startswith("ESTIMATE")
    assert details.iloc[0]["Sector_Source"] == "sharadar"
    exports.persist_summary(summary, tmp_path / "summary.csv")
    exports.persist_details(details, tmp_path / "details.csv")
    saved, _ = exports.read_details(tmp_path / "details.csv")
    assert saved.iloc[0]["Exit_Valuation"] == details.iloc[0]["Exit_Valuation"]


def test_app_generates_missing_sharadar_files_without_touching_baseline(monkeypatch, tmp_path):
    import backtest_engine as bt
    import backtest_exports as exports
    import backtest_results_page as page_module
    import sharadar_backtest
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(page_module, "require_access", lambda: None)
    monkeypatch.setattr(exports, "DATA_DIR", tmp_path)
    isolated = tmp_path / "isolated"
    monkeypatch.setattr(sharadar_backtest, "DATA_DIR", isolated)
    monkeypatch.setenv("SHARADAR_API_KEY", "fixture-only")
    baseline = tmp_path / "backtest_results.csv"
    baseline.write_text("Year,Portfolio_Return,Benchmark_Return,Alpha\n2020,0.1,0.05,0.05\n")
    before = baseline.read_bytes()

    def calculate(year, **kwargs):
        assert kwargs["provider"].sector_source == "sharadar"
        return bt.YearlyBacktest(
            {"Year": year, "Portfolio_Return": 0.2, "Benchmark_Return": 0.1, "Alpha": 0.1,
             "Sector_Source": "sharadar", "Delisting_Policy": "strict", "Estimated_Exit_Count": 0},
            [{"Year": year, "Ticker": "T0", "Weight": 1.0, "Weight_Percent": 100.0,
              "Start_Price": 50.0, "End_Price": 60.0, "Annual_Return": 0.2,
              "Portfolio_Return": 0.2, "Benchmark_Return": 0.1, "Alpha": 0.1,
              "Exit_Valuation": "Observed annual endpoint", "Sector_Source": "sharadar"}],
        )

    monkeypatch.setattr(exports, "compute_backtest_year", calculate)
    page = AppTest.from_file(str(Path(exports.__file__).parent / "pages/1_Backtest_Results.py")).run()
    page.radio[0].set_value("Sharadar historical results").run()
    page.number_input[0].set_value(2020)
    page.number_input[1].set_value(2020)
    page.button[0].click().run(timeout=20)
    assert not page.exception
    assert (isolated / "backtest_results.csv").exists()
    assert (isolated / "backtest_detailed_results.csv").exists()
    assert baseline.read_bytes() == before
    assert len(page.get("download_button")) == 2
